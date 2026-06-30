import os
import re
import redis
import anthropic
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from atlassian import Confluence
from dotenv import load_dotenv

load_dotenv()

# Get environment variables
slack_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")

if not slack_token:
    raise ValueError("SLACK_BOT_TOKEN not found")
if not signing_secret:
    raise ValueError("SLACK_SIGNING_SECRET not found")

# Initialize apps
app = App(token=slack_token, signing_secret=signing_secret)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# Initialize Confluence
confluence = Confluence(
    url="https://globant-services.atlassian.net/wiki",
    username=os.environ.get("CONFLUENCE_EMAIL"),
    password=os.environ.get("CONFLUENCE_API_TOKEN")
)

# Initialize Anthropic (Claude)
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# SD-US group ID
SD_US_GROUP_ID = "S09RRN48LDV"

# ── Redis thread persistence ──────────────────────────────────────────────────
redis_client = redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)
THREADS_KEY = "sdb:active_threads"

def is_active_thread(thread_ts):
    try:
        result = redis_client.sismember(THREADS_KEY, thread_ts)
        print(f"DEBUG - is_active_thread({thread_ts}): {result}")
        return result
    except Exception as e:
        print(f"DEBUG - is_active_thread error: {str(e)}")
        return False

def add_active_thread(thread_ts):
    try:
        redis_client.sadd(THREADS_KEY, thread_ts)
        print(f"DEBUG - Redis sadd success: {thread_ts}")
    except Exception as e:
        print(f"DEBUG - Redis sadd failed: {str(e)}")
# ─────────────────────────────────────────────────────────────────────────────

def strip_html(html):
    """Strip HTML tags and clean up text"""
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()

def extract_keywords(question):
    """Use Claude to extract search keywords from a question"""
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=50,
        messages=[
            {
                "role": "user",
                "content": f"Extract 2-3 key search words from this question. Return ONLY the words separated by spaces, nothing else: {question}"
            }
        ]
    )
    return message.content[0].text.strip()

def should_escalate(answer):
    """Check if the answer contains ESCALATE as a standalone line"""
    lines = [line.strip() for line in answer.strip().split("\n")]
    return "ESCALATE" in lines

def search_confluence(query):
    try:
        keywords = extract_keywords(query)
        print(f"DEBUG - Keywords extracted: {keywords}")
        cql_query = f"type=page AND space=GL0309 AND text~\"{keywords}\""
        print(f"DEBUG - CQL Query: {cql_query}")
        results = confluence.cql(cql_query, limit=3)
        pages = results.get("results", [])
        print(f"DEBUG - Pages found: {len(pages)}")
        if not pages:
            return None
        context = ""
        for page in pages:
            page_id = page.get("content", {}).get("id") or page.get("id")
            title = page.get("content", {}).get("title") or page.get("title", "Unknown")
            print(f"DEBUG - Processing page: {title} (ID: {page_id})")
            if not page_id:
                print("DEBUG - No page ID found, skipping")
                continue
            page_app = confluence.get_page_by_id(
                page_id, expand="body.storage"
            )
            raw_body = page_app["body"]["storage"]["value"]
            clean_body = strip_html(raw_body)[:2000]
            context += f"\n{title}\n{clean_body}\n"
        return context if context else None
    except Exception as e:
        print(f"DEBUG - Exception: {str(e)}")
        return None

def ask_claude(question, confluence_context):
    """Ask Claude with Confluence context"""
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"""You are SDB, a friendly ServiceDesk assistant for Globant.

STRICT RULES:
- Answer using the documentation provided below
- Each page in the documentation contains a Keywords section and an "Also asked as" section — use these to match the user's question to the right topic even if phrased differently
- If the documentation is about the same topic as the question, answer it confidently using the documented steps
- If the question is completely unrelated to anything in the documentation, respond with EXACTLY this on its own line: ESCALATE
- If the user says the documented steps already failed or did not work for them (e.g. "it doesn't work", "login fails", "still broken"), do NOT repeat or re-explain the steps. Instead respond with EXACTLY this on its own line: ESCALATE
- Do not make up information that is not in the documentation

IMPORTANT FORMATTING RULES:
- Do NOT use #, ##, ### for headers
- Do NOT use ** for bold
- Do NOT use markdown formatting
- Use plain text only
- Use numbers (1. 2. 3.) for steps
- Use simple dashes (-) for bullet points
- Keep emojis minimal, only use them naturally
- Keep the response concise and easy to read in Slack
- Do NOT start with greetings like "Hey", "Hi there", "Hello" — get straight to the answer

Documentation:
{confluence_context}

User Question: {question}

Respond in a friendly, professional tone."""
            }
        ]
    )
    return message.content[0].text

def handle_escalation(say, user, question, channel, thread_ts=None):
    """Handle escalation to SD-US team"""
    if thread_ts:
        say(
            text=f"I wasn't able to find documentation on that topic. Let me get the team to help you out! <@{user}>",
            thread_ts=thread_ts
        )
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"<!subteam^{SD_US_GROUP_ID}> a user needs help with: {question}"
        )
    else:
        say(text=f"I wasn't able to find documentation on that topic. Let me get the team to help! <@{user}>")
        app.client.chat_postMessage(
            channel=channel,
            text=f"<!subteam^{SD_US_GROUP_ID}> a user needs help with: {question}"
        )

def process_question(question, user, channel, thread_ts, say):
    """Process a question and respond"""
    context = search_confluence(question)

    if not context:
        handle_escalation(say, user, question, channel, thread_ts)
        return

    answer = ask_claude(question, context)
    print(f"DEBUG - Answer: {answer}")

    if should_escalate(answer):
        handle_escalation(say, user, question, channel, thread_ts)
        return

    say(text=answer, thread_ts=thread_ts)

# Handle @SDB mentions in channels
@app.event("app_mention")
def handle_mention(event, say):
    user = event["user"]
    text = event["text"]
    thread_ts = event.get("thread_ts", event["ts"])
    channel = event["channel"]
    question = text.split(">", 1)[-1].strip()

    # Track and persist this thread in Redis
    add_active_thread(thread_ts)

    say(
        text=f"Hey <@{user}>! Give me a moment while I look that up for you... 🔍",
        thread_ts=thread_ts
    )

    process_question(question, user, channel, thread_ts, say)

# Handle all messages — thread replies and DMs
@app.event("message")
def handle_message(event, say):
    if event.get("bot_id"):
        return
    if event.get("subtype"):
        return

    channel_type = event.get("channel_type")
    thread_ts = event.get("thread_ts")
    user = event.get("user")
    question = event.get("text", "")
    channel = event["channel"]

    # Handle DMs
    if channel_type == "im":
        say(text=f"Hey <@{user}>! Give me a moment while I look that up for you... 🔍")
        process_question(question, user, channel, None, say)
        return

    # Handle thread replies — only if SDB has previously responded in this thread
    if thread_ts and is_active_thread(thread_ts):
        say(text=f"Give me a moment... 🔍", thread_ts=thread_ts)
        process_question(question, user, channel, thread_ts, say)

# Flask route for Slack events
@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@flask_app.route("/health", methods=["GET"])
def health():
    return "SDB is alive! 🤖", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)