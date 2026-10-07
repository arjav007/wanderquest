# WanderQuest
AI scavenger hunts built from OpenStreetMap data. An open-weight model (gpt-oss-20b on Groq) writes the riddles; you walk, find the place, and snap a photo.

## Run locally
pip install -r requirements.txt
Create a .env file:
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-20b
LLM_API_KEY=your_key
DATABASE_URL=   (empty = local SQLite)
Then: uvicorn app:app --host 127.0.0.1 --port 8000
