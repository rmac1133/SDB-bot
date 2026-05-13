import os
import anthropic
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from atlassian import Confluence

# Initialize apps
app = App(token=os.environ["SLACK_BOT_TOKEN"],
          signing_secret=os.environ["SLACK_SIGNING_SECRET"])

flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# Initialize Confluence
confluence = Confluence(
    url="https://globant.atlassian.net/wiki",
    username=os.environ["CONFLUENCE_EMAIL"],
    password=os.environ["CONFLUENCE_API_TOKEN"]
)

# Initialize Anthropic (Claude)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def search_confluence(query):
    """Search only USA ServiceDesk Knowledge Base folder"""
    try:
        results = confluence.cql(
            f'type=page AND space=GL0309 AND ancestor="USA ServiceDesk Knowledge Base" AND text~"{query}"',
            limit=3
        )
        pages = results.get("results", [])
        if not pages:
            return "No relevant documentation found in the USA ServiceDesk Knowledge Base."

        context = ""
        for page in pages:
            title = page["title"]
            page_id = page["id"]
            content = confluence.get_page_by_id(
                page_id, expand="body.storage"
            )
            body = content["body"]["storage"]["value"][:1000]
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

    # Remove the bot mention from the text
    question = text.split(">", 1)[-1].strip()

    # Let user know we're working on it
    say(
        text=f"Hey <@{user}>! Let me check the ServiceDesk Knowledge Base for you... 🔍",
        thread_ts=thread_ts
    )