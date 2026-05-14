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
            return "No relevant documentation found in the USA ServiceDesk Knowledge Base."
        context = ""
        for page in pages:
            title = page["title"]
            page_id = page["id"]
            page_app = confluence.get_page_by_id(
                page_id, expand="body.storage"
            )
            body = page_app["body"]["storage"]["value"][:2000]
            context += f"\n**{title}**\n{body}\n"
        return context
    except Exception as e:
        print(f"DEBUG - Exception: {str(e)}")
        return f"Could not search Confluence: {str(e)}"