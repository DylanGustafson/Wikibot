#Wikibot by Dylan G. (c) 2025
import os, sys, re, time        #General stuff
import requests                 #For Wikipedia API
import sqlite3, subprocess      #For iMessage I/O

#Applescript has no protections against sending a message long enough to crash iMessage (lol). 11,000 is a safe but arbitrary choice.
IMSG_HARD_LIMIT = 11000

#Upper and lower limits on the possible lengths of article chunks. These are separate from
#IMSG_HARD_LIMIT as they only apply to article chunks BEFORE possible postscripts are added.
msg_upper_limit = 10000
msg_lower_limit = 100

#Default limit on text length for actual article chunks, overridden by msg_upper_limit and IMSG_HARD_LIMIT
default_limit = 2000

#Global switch to enable/disable on all chats & group chats
enabled = True

#Undocumented iMessage database stuff that could change
chat_db_path = "~/Library/Messages/chat.db"
direct_chat_prefix = "iMessage;-;"
group_chat_prefix = "iMessage;+;"
sql_max_rowid = "SELECT MAX(rowid) FROM message"
sql_get_new = ("SELECT H.id, text, is_from_me, cache_roomnames"
               " FROM message M LEFT JOIN handle H"
               " ON H.rowid=M.handle_id"
               " WHERE M.rowid=")

#User agent spoof; not actually needed for this API
req_header = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X x.y; rv:42.0) Gecko/20100101 Firefox/42.0"}

#Global variable that contains article data for all users, including this example entry
wiki_data = {"Example User":
                {"title": "Example Article Title",
                 "toc": ["Introduction", "History"],
                 "sections": ["Example introduction", "Example of history section"],
                 "section_num": 0,
                 "chunks": ["Example introduction"],
                 "chunk_num": 0,
                 "limit": default_limit,
                 "disambig": False,
                 "links": ["Page link 1", "Page link 2", "Page link 3"]}}

#
# Core functions
#

def main():
    #Jump to command line mode if CLI flag was passed
    if sys.argv[-1].lower() == "cli":
        return cli()

    #Connect to local iMessage database
    conn = sqlite3.connect(os.path.expanduser(chat_db_path))
    cur = conn.cursor()

    #Get starting ROWID
    rowid = 1 + cur.execute(sql_max_rowid).fetchone()[0]
    print("Database loaded! Waiting for new messages.")

    #Main loop - Monitor chat db for new messages and send responses
    while True:
        #Attempt to load new message from chat.db
        new_data = cur.execute(sql_get_new + str(rowid)).fetchone()

        #No new message or message hasnt finished downloading - wait 1 second and repeat
        if new_data == None or any(col == None for col in new_data[:-1]):
            time.sleep(1)
            continue

        #Load contents into variables and update rowid
        handle, text, is_from_me, room_name = new_data
        rowid += 1

        #If latest message is from bot itself, ignore it
        if is_from_me == 1:
            continue

        #If group chat: "iMessage;+;[group chat ID]", otherwise: "iMessage;-;[phone #]"
        user = group_chat_prefix + room_name if room_name else direct_chat_prefix + handle

        #Form response message
        try:
            response = get_response(text, user)
        except Exception as err:
            log(f"Unhandled exception creating response: {err}")
            continue

        #Nothing to send or bot is disabled - continue
        if response == None:
            continue

        #Hard limit on length in case a long message is sent erroneously
        if len(response) > IMSG_HARD_LIMIT:
            response = response[:IMSG_HARD_LIMIT - 2] + "..."
            log("iMessage hard limit exceeded")

        #Send iMessage via Applescript. Using "on run()" to pass in response & user is safer than building a single "tell" statement
        subprocess.run(["osascript",
                        "-e", "on run(msg, target)",
                        "-e", "tell application \"Messages\" to send msg to chat id target",
                        "-e", "end run",
                        response, user])

#Command-line interface mode for testing
def cli():
    print("Wikibot CLI mode - use 'q' to quit")
    text = ""
    while True:
        text = input("> ")
        if text == "q":
            return

        response = get_response(text, "local")
        if response != None:
            print(f"\033[92m{response}\033[0m")

#Get text response from text input. Not iMessage-specific
def get_response(msg, user):
    msg = msg.strip()

    #If not enabled, check if its the special reactivation text, other return None
    if not enabled:
        return cmd_enable() if msg.lower() == "wikibot enable" else None

    #If msg can be cast to an int, interpret as a page link if on a disambiguation page, otherwise section number
    if user in wiki_data and cast_int(msg) != None:
        return cmd_link(msg, user) if wiki_data[user]["disambig"] else cmd_sect(msg, user)

    #Separate command from arguments by splitting at first non-alphanumeric char (keeps all characters)
    cmd_text, arg_text = re.split(r"(?![A-Za-z0-9])", msg, 1)
    cmd_text = cmd_text.lower()
    arg_text = arg_text.lstrip()

    #Resolve aliases
    if cmd_text in aliases:
        cmd_text = aliases[cmd_text]

    if cmd_text not in commands:
        return f"Command not found! Type {stylize_text('help', 'bold sans')} for a list of commands."

    #Run specified command using "commands" dictionary
    response = commands[cmd_text]["func"](arg_text, user)
    return response.strip()

#
# Command functions
#

#Request page via Wikipedia TextExtracts API and organize into and entry for wiki_data
def cmd_search(title, user):
    if title == "":
        return cmd_help("search", user)

    #Set up wikipedia API query
    wiki_url = "https://en.wikipedia.org/w/api.php"
    req_params = {
        "action": "query",
        "format": "json",
        "prop": "extracts|pageprops",
        "ppprop": "disambiguation",
        "redirects" : "1",
        "explaintext": "1",
        "titles": title}

    #Get API response
    try:
        req = requests.get(wiki_url, params=req_params, headers=req_header)
    except Exception as err:
        log(err)
        return "Wikipedia won't talk to me :'^("

    #Check if response matches expected format
    try:
        json_data = req.json()["query"]
        if int(list(json_data["pages"].keys())[0]) == -1:
            return "Page does not exist! :-("

        page_data = list(json_data["pages"].values())[0]
        new_title = page_data["title"]
        new_extract = page_data["extract"]
    except Exception as err:
        log(err)
        return "I can't even tell what Wikipedia sent me =^("

    #Non-cached article preview that displays after first retrieving an article
    preview = "\n"

    #Check if this is a disambiguation page containing monstly links
    new_links = []
    new_disambig = "pageprops" in page_data

    #Disambiguation page: split along page choices
    if new_disambig:
        new_extract, new_links = get_disambig_links(new_extract, new_title)

        #Add "disambiguation" to title along with instructions (not cached)
        new_title += " (Disambiguation)"
        preview += stylize_text("Enter an item number to be taken to its article\n\n", "italic sans")

    #Normal article: add redirect notice if applicable
    elif "redirects" in json_data:
        preview += f"(Redirected from {title})\n\n"

    #Split along major sections and extract TOC
    new_toc, new_sections = organize_sections(new_extract)

    #Use default character limit if user is new
    new_limit = wiki_data[user]["limit"] if user in wiki_data else default_limit

    #Save page data into local cache for subsequent use
    wiki_data.update({user: {
        "title": stylize_text(new_title, "bold serif"),
        "toc": new_toc,
        "sections": new_sections,
        "section_num": 0,
        "limit": new_limit,
        "disambig": new_disambig,
        "links": new_links}})

    #Article preview: Full text if disambiguation page, TOC for normal articles
    preview += cmd_all("", user) if new_disambig else get_short_toc(user)

    #Include total article length in kB at end of preview (not cached)
    total = len("".join(new_sections)) / 1000
    total = round(total, 1) if total < 10 else int(total)
    preview += f"\n\nTotal: {total} kB"

    #Display title and non-cached preview as first response
    return wiki_data[user]["title"] + preview

#Enumerate links and extract page names from disambiguation page text
def get_disambig_links(extract, title):
    #Chop off just before "see also" section
    extract = extract.split("==.?See also.?==")[0].strip()

    #Split along non-header text (doesnt start with '=') that's preceded by a newline and followed by a newline or comma
    link_parts = re.split(r"(?<=\n)(?!\=)(.+?)(?=,|\n)", extract)

    #Extract page names into TOC, and number them in the article text. Even elements contain the text between page links
    new_links = []
    for i in range(1, len(link_parts), 2):
        #Get page name but remove all double quotes
        page_name = re.sub('"|“|”', '', link_parts[i])

        #If page name is just the article title, add the next group after its comma (comma in page name, ie a placename)
        if page_name.lower() == title.lower():
            new_match = re.search(r",(.+?)(?=,|\n)", link_parts[i + 1])
            page_name += new_match.group() if new_match else ""

        #Add page name to TOC
        new_links += [page_name]

        #Insert numbering next to each page name, starting at 1
        link_parts[i] = f"{str(len(new_links))}. {link_parts[i]}"

    #Recombine link_parts as new extract
    return "".join(link_parts), new_links

#Organize page text from TextExtracts API into separate sections and titles - Internal use only
def organize_sections(extract):
    #Split by major sections (delimited by ==). Add section name "Introduction" at the start
    sect_parts = ["Introduction"] + re.split(r"(?<!\=)==(?!\=)", extract)

    #Extract section titles and organize content up through the final sections that arrive empty (see also/references)
    new_toc = []
    new_sections = []
    for i in range(0, len(sect_parts), 2):
        sect_title = sect_parts[i].strip()
        if "See also" in sect_title or "References" in sect_title:
            break

        #Odd-indexed elements contain actual section text
        sect_text = sect_parts[i + 1].strip()

        #Turn subsection titles (===Title===) bold, and sub-subsection titles (====Title====) bold italic
        sect_text = format_headers(sect_text, "===", "bold sans")
        sect_text = format_headers(sect_text, "====", "bold italic sans")

        new_toc += [sect_title]
        new_sections += [stylize_text(sect_title.upper(), "bold serif") + "\n\n" + sect_text]

    return new_toc, new_sections

#Replaces wiki-formatted headers (==Title==, ===Title===, ====Title====) with stylized text and newlines
def format_headers(extract, delimiter, style):
    #Split strictly along header delimiter (=== or ====). Regex is used so === can be handled before ====
    sub_parts = re.split(f"(?<!\\=){delimiter}(?!\\=)", extract)

    #Strip each part and stylize every other to create formatted headers
    for i in range(len(sub_parts)):
        sub_parts[i] = sub_parts[i].strip()
        if i % 2 == 1:
            sub_parts[i] = "\n" + stylize_text(sub_parts[i], style)
        elif i > 0 and sub_parts[i] == "":
            sub_parts[i] = stylize_text("-subsection contains no text-", "italic sans")

    return "\n".join(sub_parts)

#link command, used to open links via numerical input from a disambiguation page (not actually in cmd list)
def cmd_link(msg, user):
    page_num = cast_int(msg)
    if page_num == None:
        return "Please input a number"

    #Compare input to total number of saved links
    total = len(wiki_data[user]["links"])
    if total == 0:
        return "There are no numbered links in this article!"

    if page_num > total or page_num < 1:
        return f"Please enter a value between 1 and {total}"

    #Search for specified page title, found in TOC
    return cmd_search(wiki_data[user]["links"][page_num - 1], user)

#Display article title and numbered table of contents
def cmd_toc(arg, user):
    if user not in wiki_data:
        return no_article()

    return wiki_data[user]["title"] + "\n" + get_highlight_toc(user)

#Get enumerated table of contents only - Internal use only
def get_short_toc(user):
    return "\n".join([f"{i}. {name}" for i, name in enumerate(wiki_data[user]["toc"])])

#Get TOC with current section highlighted in bold - Internal use only
def get_highlight_toc(user):
    toc_list = get_short_toc(user).split("\n")
    num = wiki_data[user]['section_num']

    toc_list[num] = stylize_text(toc_list[num], "bold sans")
    return "\n".join(toc_list)

#Get next chunk in section, or start next section
def cmd_next(arg, user):
    if user not in wiki_data:
        return no_article()

    #Haven't loaded any sections yet, start with Introduction
    if "chunks" not in wiki_data[user]:
        load_sect(0, user)

    #End of section reached, load next section. Wraps around to beginning if needed.
    elif wiki_data[user]["chunk_num"] >= len(wiki_data[user]["chunks"]) - 1:
        new_sect_num = (wiki_data[user]["section_num"] + 1) % len(wiki_data[user]["sections"])
        load_sect(new_sect_num, user)

    #Otherwise simply increment chunk number
    else:
        wiki_data[user]["chunk_num"] += 1

    #Send requested section chunk
    return get_current(user)

#Get previous chunk in section, or start previous section
def cmd_prev(arg, user):
    if user not in wiki_data:
        return no_article()

    #Haven't loaded any sections yet or beginning of section reached, start with final chunk of previous section
    if "chunk_num" not in wiki_data[user] or wiki_data[user]["chunk_num"] <= 0:
        new_sect_num = (wiki_data[user]["section_num"] - 1) % len(wiki_data[user]["sections"])
        load_sect(new_sect_num, user)
        wiki_data[user]["chunk_num"] = len(wiki_data[user]["chunks"]) - 1

    #Otherwise simply decrement chunk number
    else:
        wiki_data[user]["chunk_num"] -= 1

    #Send requested section chunk
    return get_current(user)

#Jump to specified major section
def cmd_sect(arg, user):
    if user not in wiki_data:
        return no_article()

    #Return current section number with name highlighted in TOC
    if arg == "?":
        return f"Currently in Section {wiki_data[user]['section_num']}\n\n{get_highlight_toc(user)}"

    #Another way to get all sections. This feature should be removed if there are articles with single-word sections titled "All"
    if arg == "all":
        return cmd_all("", user)

    #Find section by number
    num = cast_int(arg)
    if num != None:
        num = max(num, 0)

        message = ""
        if num >= len(wiki_data[user]["sections"]):
            num = len(wiki_data[user]["sections"]) - 1
            message = stylize_text(f"Section number too large, jumping to Section {num} instead", "italic sans") + "\n\n"

        load_sect(num, user)
        return message + get_current(user)

    #Find section by name
    arg = arg.lower()
    for i, name in enumerate(wiki_data[user]["toc"]):
        if name.lower().startswith(arg):
            load_sect(i, user)
            return get_current(user)

    return "Section not found!\n\n" + get_short_toc(user)

#Jump to specified section number and split into chunks using character limit - Internal use only
def load_sect(number, user):
    #Special case -1: Concatenate sections and load these into the chunks
    if number == -1:
        #Sections separated by double newlines for disambiguation page, triple newline for normal article
        newlines = "\n\n" if wiki_data[user]["disambig"] else "\n\n\n"

        #Newlines plus invisible char <ascii 1> to help track section number in TOC
        section = (newlines + chr(1)).join(wiki_data[user]["sections"])
        number = 0

        #For disambiguation pages, chop off "Introduction" text
        if wiki_data[user]["disambig"]:
            section = section.split("\n\n", 1)[-1]

    #Otherwise load specified section number
    else:
        section = wiki_data[user]["sections"][number]

    #Split section into chunks of specified max size, while avoiding cutting up words
    chunks = re.findall(f"(.{{1,{wiki_data[user]['limit']}}})(?=\\b)", section, re.DOTALL)

    #Add (i/n) postscripts after each chunk
    n = len(chunks)
    for i in range(n):
        chunks[i] = chunks[i].rstrip() + f" ({i + 1}/{n})"

    #Update wiki_data with new section chunks
    wiki_data[user].update({"section_num": number, "chunks": chunks, "chunk_num": 0})

#Get text of current chunk - Internal use only
def get_current(user):
    response = wiki_data[user]["chunks"][wiki_data[user]["chunk_num"]]

    #If viewing article all at once, use chr(1)'s to update current section number
    wiki_data[user]["section_num"] += response.count(chr(1))

    #Include end-of-article postscript if applicable
    if wiki_data[user]["section_num"] >= len(wiki_data[user]["sections"]) - 1 and wiki_data[user]["chunk_num"] >= len(wiki_data[user]["chunks"]) - 1:
        response += stylize_text("\n\n[END OF ARTICLE]", "bold sans")

    return response

#Load entire article text into "chunks"
def cmd_all(arg, user):
    if user not in wiki_data:
        return no_article()

    load_sect(-1, user)
    return get_current(user)

#Load chunk specified by numerical arg or by keyword
def cmd_part(arg, user):
    if user not in wiki_data:
        return no_article()

    #Specific keywords
    arg = arg.lower()
    if "next".startswith(arg):
        return cmd_next("", user)
    if "previous".startswith(arg):
        return cmd_prev("", user)
    if "first".startswith(arg):
        wiki_data[user]["chunk_num"] = 0
        return get_current(user)
    if "last".startswith(arg):
        wiki_data[user]["chunk_num"] = len(wiki_data[user]["chunks"]) - 1
        return get_current(user)

    #Find part by number. Unlike section numbers, part numbers start at 1 instead of 0
    num = cast_int(arg)
    if num != None:
        num = max(num, 1)
        message = ""

        #Cap part # request at maximum (final)
        if num > len(wiki_data[user]["chunks"]):
            num = len(wiki_data[user]["chunks"])
            message = stylize_text(f"Part number too large, jumping to part {num} instead", "italic sans") + "\n\n"

        #Decrease by 1 to convert to zero based index
        wiki_data[user]["chunk_num"] = num - 1
        return message + get_current(user)

    #Give up message
    return "Part not found! Use a number or the keywords next/previous/first/last"

#Set max character limit, overridden by msg_upper_limit
def cmd_limit(arg, user):
    if user not in wiki_data:
        return no_article()

    #No arg passed - display current limit and some help text
    if arg == "":
        return f"Current character limit is {wiki_data[user]['limit']}. Use " + \
               stylize_text("limit ", "bold sans") + stylize_text("value", "bold italic sans") + " to modify."

    #Interpret arg as number or keyword "default"
    num = cast_int(arg)
    if num != None:
        new_lim = num
    elif "default".startswith(arg.lower()):
        new_lim = default_limit
    else:
        return "Please enter an actual number, like this:\n" + commands["limit"]["examp"]

    #Check inputted value against upper/lower limits
    if new_lim > msg_upper_limit:
        new_lim = msg_upper_limit
        response = f"Maximum upper limit is {new_lim}, updating to this value"
    elif new_lim < msg_lower_limit:
        new_lim = msg_lower_limit
        response = f"Minimum lower limit is {new_lim}, updating to this value"
    else:
        response = f"New character limit set to {new_lim}"

    wiki_data[user]["limit"] = new_lim
    return response + ". Change will take effect when a new section or article is loaded."

#Error message when no article has been loaded first
def no_article():
    style = "bold italic sans"

    #Include alias for "search" help text if it exists
    alias_keys = list(aliases.keys())
    aliased_cmds = list(aliases.values())
    or_alias = " or " + stylize_text(alias_keys[aliased_cmds.index("search")], style) if "search" in aliased_cmds else ""

    return f"No article loaded! Use {stylize_text('search', style)}{or_alias} to find an article first."

#Delete article data for given user
def cmd_clear(arg, user):
    if user not in wiki_data:
        return "No article loaded!"

    del wiki_data[user]
    return "Article cache cleared ;^)"

#Disable wikibot for all users
def cmd_disable(arg, user):
    global enabled
    enabled = False

    log("Wikibot disabled")
    return f"Wikibot disabled. Type " + stylize_text("wikibot enable", "bold sans") + " to re-enable."

#Enable wikibot. Requires no arguments since it is only called directly as a special case
def cmd_enable():
    global enabled
    enabled = True

    log("Wikibot enabled")
    return "Wikibot now enabled"

#Get help text
def cmd_help(arg, user):
    #Resolves aliases except "" for "next"
    if arg in aliases and arg != "":
        arg = aliases[arg]

    #Help with specific command in list? Just return usage & examples for that command
    if arg in commands:
        info = commands[arg]
        response = info["usage"] if "usage" in info else "This command takes no arguments"

        if "examp" in info:
            response += "\n\nExample(s):\n" + info["examp"]

        return response

    #If some other arg was passed, give error message before listing command
    elif arg != "":
        response = "Command not found!"

    #If no arg was passed, include additional instructions for the help command itself, then list commands
    else:
        response = f"Type {stylize_text('help', 'bold sans')} {stylize_text('command', 'bold italic sans')} to see specific command usage."

    response += "\n\n" + stylize_text("COMMAND LIST", "bold sans")

    #For pulling current aliases
    alias_keys = list(aliases.keys())
    aliased_cmds = list(aliases.values())

    #Pull command names and aliases from the respective dictionaries, along with their descriptions
    for cmd_name, info in commands.items():
        response += "\n" + stylize_text(cmd_name, "bold sans")

        #Include alias if it exists. Only prints the first alias for each command
        if cmd_name in aliased_cmds:
            #Retrieve alias name and make bold. If its blank, just write quotes
            alias = alias_keys[aliased_cmds.index(cmd_name)]
            alias = "“”" if alias == "" else stylize_text(alias, "bold sans")
            response += f" (or {alias})"

        #Add command descriptions
        response += " - " + info["desc"]

    return response

#Ping command for testing connection & response
def cmd_ping(arg, user):
    #Echo alphanumeric message with a dividing space, otherwise no space
    if arg and arg[0].isalnum():
        arg = " " + arg

    return "pong" + arg

#
# Misc. utility functions
#

#Print message or exception to console with timestamp
def log(msg):
    print(time.strftime("%F %T") + f" - {msg}")

#Attempt to cast text as integer
def cast_int(text):
    try:
        return int(float(text))
    except ValueError:
        return None

#Stylize alphanumeric plain text into bold, italic, and/or serif using UTF-8 mathematical characters. Cannot italicize numbers.
def stylize_text(text, style_name):
    #syntax:  "style name":        [(0-9) , (A-Z) , (a-z) , [(exceptions initial)], [(exceptions final)]]
    styles = {"bold sans":         [120764, 120211, 120205],
              "italic sans":       [     0, 120263, 120257],
              "bold italic sans":  [120764, 120315, 120309],
              "bold serif":        [120734, 119743, 119737],
              "italic serif":      [     0, 119795, 119789, [104], [8462]],
              "bold italic serif": [120734, 119847, 119841],
              "doublestruck":      [120744, 120055, 120049, [67, 72, 78, 80, 81, 82, 90], [8450, 8461, 8469, 8473, 8474, 8477, 8484]]}

    #Load offset values based on style name
    offsets = styles[style_name]

    #Loop through each character and replace alphanumeric chars with stylized symbols
    new_text = ""
    for i in text:
        id = ord(i)
        if len(offsets) > 3 and id in offsets[3]:  #exceptions that cannot be found using a simple offset
            id = offsets[4][offsets[3].index(id)]
        elif id >= 48 and id <= 57:                # 0-9
            id += offsets[0]
        elif id >= 65 and id <= 90:                # A-Z
            id += offsets[1]
        elif id >= 97 and id <= 122:               # a-z
            id += offsets[2]
        new_text += chr(id)

    return new_text

#Command list. Keys represent the text input, while values contain the function to call as well as help info
commands = {
    "search": {
        "func": cmd_search,
        "desc": "Search for an article by title",
        "usage": stylize_text("search ", "bold sans") + stylize_text("article title", "bold italic sans") + "\n" + \
                 stylize_text("get ", "bold sans") + stylize_text("article title", "bold italic sans"),
        "examp": "search Pishpek\nget mauna kea"},
    "toc": {
        "func": cmd_toc,
        "desc": "Print the table of contents"},
    "next": {
        "func": cmd_next,
        "desc": "Get the next part of the article",
        "usage": f"Simply type \"next\" or use {stylize_text('return + send', 'italic sans')} to send a blank text"},
    "previous": {
        "func": cmd_prev,
        "desc": "Get the previous part of the article"},
    "section": {
        "func": cmd_sect,
        "desc": "Jump to a specified section (by name or number)",
        "usage": stylize_text("section ", "bold sans") + stylize_text("name／number", "bold italic sans") + "\n" + \
                 stylize_text("sect ", "bold sans") + stylize_text("name／number", "bold italic sans") + "\n\n" + \
                 "Alternatively just type the section number itself.",
        "examp": "section 3\nsect history\n4\nsect?"},
    "part": {
        "func": cmd_part,
        "desc": "Jump to a specified part of a section",
        "usage": stylize_text("part ", "bold sans") + stylize_text("number", "bold italic sans") + "\n\n" \
                 + "Alternatively use keywords next/previous/first/last",
        "examp": "part 3\npart last"},
    "all": {
        "func": cmd_all,
        "desc": "Get entire article text, rather than split by sections"},
    "limit": {
        "func": cmd_limit,
        "desc": "Set soft character limit for response messages",
        "usage": stylize_text("limit ", "bold sans") + stylize_text("value", "bold italic sans") + "\n" + \
                 stylize_text("lim ", "bold sans") + stylize_text("value", "bold italic sans") + "\n\n" + \
                 f"Set to {default_limit} by default, maximum is {msg_upper_limit}",
        "examp": "limit 3000\nlim 1500\nlim default"},
    "clear": {
        "func": cmd_clear,
        "desc": "Clear out the cache for your article"},
    "disable": {
        "func": cmd_disable,
        "desc": "Disable wikibot"},
    "help": {
        "func": cmd_help,
        "desc": "Print the help text",
        "usage": stylize_text("help ", "bold sans") + stylize_text("command", "bold italic sans"),
        "examp": "help search"},
    "ping": {
        "func": cmd_ping,
        "desc": "Ping bot to check connection"}}

#Short form aliases for the important commands. Only the first alias of a given command will show up in the help text
aliases = {
    "get":      "search",
    "":         "next",
    "prev":     "previous",
    "sect":     "section",
    "lim":      "limit",
    "stop":     "disable"}

if __name__ == "__main__":
    main()
