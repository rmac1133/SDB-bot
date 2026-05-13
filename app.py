import os
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
    url="https://globant.atlassian.net/wiki",
    username=os.environ.get("CONFLUENCE_EMAIL"),
    password=os.environ.get("CONFLUENCE_API_TOKEN")
)

# Initialize Anthropic (Claude)
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def search_confluence(query):
    """Search only USA ServiceDesk Knowledge Base folder"""
    try:
        results = confluence.cql(
            f'type=page AND space=GL0309 AND ancestor=2681012313 AND text~"{query}"',
            limit=3
        )
        pages = results.get("results", [])
        if not pages:
            return "No relevant documentation found in the USA ServiceDesk Knowledge Base."

        context = ""
        for page in pages:
            title = page["title"]
            page_id = page["id"]
            page_app = confluence.get_page_by_id(
                page_id, expand="body.storage"
            )
            body = page_app["body"]["storage"]["value"][:1000]
            context += f"\n**{title}**\n{body}\n"
        return context
    except Exception as e:
        return f"Could not search Confluence: {str(e)}"

def ask_claude(question, confluence_context):
    """Ask Claude with Confluence context"""
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"""You are SDB, a friendly ServiceDesk assistant for Globant.
Use the following documentation from the USA ServiceDesk Knowledge Base to answer the question.
If you cannot find the answer in the documentation, let the user know you will escalate to the team.
Do not make up answers. Only use what is in the documentation provided.

Documentation:
{confluence_context}

User Question: {question}

Respond in a friendly, professional tone. Keep it concise and clear."""
            }
        ]
    )
    return message.content[0].text

# Handle @SDB mentions in channels
@app.event("app_mention")
def handle_mention(event, say):
    user = event["user"]
    text = event["text"]
    thread_ts = event.get("thread_ts", event["ts"])
    question = text.split(">", 1)[-1].strip()

    say(
        text=f"Hey <@{user}>! Let me check the ServiceDesk Knowledge Base for you... 🔍",
        thread_ts=thread_ts
    )

    context = search_confluence(question)
    answer = ask_claude(question, context)

    if "escalate" in answer.lower():
        say(
            text=f"{answer}\n\nEscalating to the ServiceDesk team for further assistance.",
            thread_ts=thread_ts
        )
    else:
        say(text=answer, thread_ts=thread_ts)

# Handle Direct Messages
@app.event("message")
def handle_dm(event, say):
    if event.get("channel_type") == "im":
        user = event["user"]
        question = event["text"]

        say(text=f"Hey <@{user}>! Let me check that for you... 🔍")

        context = search_confluence(question)
        answer = ask_claude(question, context)
        say(text=answer)

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