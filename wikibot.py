#Wikibot by Dylan G
import os, sys, re, time        #General stuff
import requests                 #For Wikipedia API
import sqlite3, subprocess      #For iMessage I/O

#Applescript has no protections against sending a message long enough to crash iMessage (lol). 10100 is a safe but arbitrary choice.
MSG_HARD_LIMIT = 10100

#Upper and lower limits on the possible lengths of article chunks. These are separate from
#MSG_HARD_LIMIT as they only apply to article chunks BEFORE possible postscripts are added.
msg_upper_limit = 10000
msg_lower_limit = 100

#Default limit on text length for actual article chunks, overridden by msg_upper_limit and MSG_HARD_LIMIT
default_limit = 2000

#Global switch to enable/disable on all chats & group chats
enabled = True

#Undocumented iMessage database stuff that could change
chat_db_path = "~/Library/Messages/chat.db"
group_chat_prefix = "iMessage;+;"
message_table = "message"
handle_table = "handle"
sql_max_rowid = f"SELECT MAX(rowid) FROM {message_table}"
sql_chk_new = f"SELECT is_from_me FROM {message_table} WHERE rowid="
sql_load_new = ("SELECT h.id, cache_roomnames, text"
               f" FROM {message_table} m LEFT JOIN {handle_table} h"
                " ON h.rowid=m.handle_id"
                " WHERE m.rowid=")

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
                 "limit": default_limit}}

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
        #Check for new message in chat.db
        new_msg = cur.execute(sql_chk_new + str(rowid)).fetchone()

        #No new message; wait 0.5 seconds and repeat
        if new_msg == None:
            time.sleep(1)
            continue

        #Update rowid
        rowid += 1

        #If latest message is from bot itself, ignore it
        if new_msg[0] == 1:
            continue

        #Get text message contents and sender's id
        (msg, user) = get_msg(cur, rowid - 1)
        if msg == None:
            continue

        #Form response message
        try:
            response = get_response(msg, user)
        except Exception as err:
            log(f"Unhandled exception creating response: {err}")

        #Nothing to send or bot is disabled - continue
        if response == None:
            continue

        #Send response to the proper chat
        try:
            send_msg(response, user)
        except Exception as err:
            log(f"Unhandled exception sending message: {err}")

#Command-line interface mode for testing
def cli():
    print("Wikibot CLI mode - use 'q' to quit")
    msg = ""
    while True:
        msg = input("> ")
        if msg == "q":
            return

        response = get_response(msg, "local")
        if response != None:
            print(f"\033[92m{response}\033[0m")

#Load and parse iMessage-specific components of new message
def get_msg(cur, rowid):
    #Select relevant columns; if they are empty, delay and retry
    max_retries = 3
    for i in range(max_retries):
        #Attempt to load message contents from chat.db
        (user, group_chat, msg) = cur.execute(sql_load_new + str(rowid)).fetchone()
        if user != None and msg != None:
            break
        log(f"Text attempt {i + 1} failed")
        time.sleep(3)

    #Give up by sending back None's
    if msg == None:
        log(f"Gave up! Rowid = {rowid}")
        return (None, None)

    #Group chats are handled slightly differently when sending text via Applescript
    if group_chat:
        user = group_chat_prefix + group_chat

    return (msg, user)

#Get text response from text input. Not iMessage-specific
def get_response(msg, user):
    msg = msg.strip()

    #Special cases
    #Sending command to re-enable - only thing that is always checked
    if msg.lower() == "wikibot enable":
        return cmd_enable()
    #Return now if enabled flag is not set
    if not enabled:
        return None
    #If msg can be cast to an int, intepret as a section number
    if cast_int(msg) != None:
        return cmd_sect(msg, user)

    #Separate command from arguments by splitting at first non-alphanumeric char (keeps all characters)
    (cmd_text, arg_text) = re.split(r"(?![A-Za-z0-9])", msg, 1)
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

#Send iMessage text to specified target (phone number, email, or group chat id)
def send_msg(msg, target):
    #Hard limit on length in case a long message is sent erroneously
    if len(msg) > MSG_HARD_LIMIT:
        msg = msg[:MSG_HARD_LIMIT - 3] + "..."

    #Escape all backslashes and quotation marks before building command
    msg = msg.replace("\\", "\\\\")
    msg = msg.replace('"', '\\"')

    #Build Applescript command to send message
    send_cmd = f"tell application \"Messages\" to send \"{msg}\" to "

    #Group chats and direct messages are handled differently
    if target.startswith(group_chat_prefix):
        send_cmd += f"(a reference to «class imct» id \"{target}\")"
    else:
        send_cmd += f"buddy \"{target}\" of (first service whose service type = iMessage)"

    #Run Applescript send command
    subprocess.run(["osascript", "-e", send_cmd])

#
# Command functions
#

#Request page via Wikipedia TextExtracts API and organize into wiki_data
def cmd_search(title, user):
    if title == "":
        return cmd_help("search", user)

    #Set up wikipedia API query
    wiki_url = "https://en.wikipedia.org/w/api.php"
    req_params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "redirects" : "1",
        "explaintext": "1",
        "titles": title}

    #Get API response
    try:
        req = requests.get(wiki_url, params = req_params, headers = req_header)
        #log("Fetched JSON from " + req.url)
    except Exception as err:
        log(err)
        print(req_params)
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

    #Get redirect text if applicable. This is not cached; it's only displayed when first loading an article
    redirect_text = "\n"
    if "redirects" in json_data:
        redirect_text += f"(Redirected from {title})\n\n"

    #Get TOC and Sections as separate arrays
    (new_toc, new_sections) = organize_page(new_extract)

    #Save page data into cache for subsequent use
    wiki_data.update({user: {
        "title": stylize_text(new_title, "bold serif"),
        "toc": new_toc,
        "sections": new_sections,
        "section_num": 0,
        "limit": default_limit}})

    #Display title with redirect and short TOC as first response
    return wiki_data[user]["title"] + redirect_text + get_short_toc(user)

#Organize page text from TextExtracts API into separate sections and titles - Internal use only
def organize_page(extract):
    #Add newlines erroneously removed by API. Capital letters that immediately follow '.' get
    #inserted newlines, unless followed by another '.', such as 'C.E.'
    #Update - this API bug appears to be fixed now
    #extract = re.sub(r"\.(?=[A-Z]?!\.)", ".\n\n", extract)

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

    return (new_toc, new_sections)

#Replaces wiki-formatted subsection titles (===Title=== or ====Title====) with stylized text and newlines
def format_headers(extract, delimiter, style):
    #Split strictly along header delimiter (=== or ====). Regex is used so === can be handled before ====
    sub_parts = re.split(f"(?<!\\=){delimiter}(?!\\=)", extract)

    #Strip each part and stylize every other to create formatted hearders
    for i in range(len(sub_parts)):
        sub_parts[i] = sub_parts[i].strip()
        if i % 2 == 1:
            sub_parts[i] = "\n" + stylize_text(sub_parts[i], style)
        elif i > 0 and sub_parts[i] == "":
            sub_parts[i] = stylize_text("-subsection contains no text-", "italic sans")

    return "\n".join(sub_parts)

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
        #Triple newlines plus invisible char <ascii 1> to help track section number
        section = ("\n\n\n" + chr(1)).join(wiki_data[user]["sections"])
        number = 0

    #Otherwise load specific section number
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

        if num > len(wiki_data[user]["chunks"]):
            num = len(wiki_data[user]["chunks"])
            message = stylize_text(f"Part number too large, jumping to part {num} instead", "italic sans") + "\n\n"

        #Decrease by 1 to convert to zero based index
        wiki_data[user]["chunk_num"] = num - 1
        return message + get_current(user)

    #Give up message
    return "Part not found! Use numbers or the keywords next/previous/first/last"

#Set max character limit, overrided by msg_upper_limit
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
    return response + ". Change will take effect when a new section or article is loaded"

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
    return f"Wikibot disabled. Type " + stylize_text("wikibot enable", "bold sans") + " to re-enable."

#Enable wikibot. Requires no arguments since it is only called directly as a special case
def cmd_enable():
    global enabled
    enabled = True
    return "Wikibot now enabled"

#Get help text
def cmd_help(arg, user):
    #Resolves aliases except "" for "next"
    if arg in aliases and arg != "":
        arg = aliases[arg]

    #Help with specific command in list?
    if arg in commands:
        info = commands[arg]
        response = info["usage"] if "usage" in info else "This command takes no arguments"

        if "examp" in info:
            response += "\n\nExample(s):\n" + info["examp"]

        return response

    #If some other arg was passed, give error message and command list only
    elif arg != "":
        response = "Command not found!"

    #If no arg was passed, include additional instructions for the help command itself
    else:
        response = f"Type {stylize_text('help', 'bold sans')} {stylize_text('command', 'bold italic sans')} to see specific command usage."

    response += "\n\n" + stylize_text("COMMAND LIST", "bold sans")

    alias_keys = list(aliases.keys())
    aliased_cmds = list(aliases.values())

    for cmd_name, info in commands.items():
        response += "\n" + stylize_text(cmd_name, "bold sans")

        if cmd_name in aliased_cmds:
            alias = stylize_text(alias_keys[aliased_cmds.index(cmd_name)], "bold sans")
            alias = "“”" if alias == "" else alias
            response += f" (or {alias})"

        response += " - " + info["desc"]

    return response

def cmd_ping(arg, user):
    return "pong!"

#
# Misc. utility functions
#

#Print message to console with datetime stamp
def log(msg):
    print(time.strftime("%F %T") + f" - {msg}")

#Attempt to cast text as integer
def cast_int(text):
    try:
        return int(float(text))
    except ValueError:
        return None

#Stylize alphanumeric plain text into bold, italic, and/or serif using UTF-8 mathematical characters. Cannot italicize numbers.
def stylize_text(text, style_name="bold sans"):
    #syntax:  "style name":        [(0-9) , (A-Z) , (a-z) , [(exceptions initial)], [(exceptions final)]]
    styles = {"bold sans":         [120764, 120211, 120205],
              "italic sans":       [     0, 120263, 120257],
              "bold italic sans":  [120764, 120315, 120309],
              "bold serif":        [120734, 119743, 119737],
              "italic serif":      [     0, 119795, 119789, [104], [8462]],
              "bold italic serif": [120734, 119847, 119841],
              "doublestruck":      [120744, 120055, 120049, [67, 72, 78, 80, 81, 82, 90], [8450, 8461, 8469, 8473, 8474, 8477, 8484]]}

    #Load offset values based on style name
    if style_name not in styles:
        return text
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
        "desc": "Set character limit for response messages",
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
