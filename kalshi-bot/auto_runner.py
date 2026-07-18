import os
import sys
from google import generativeai as genai

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY not found in environment variables.")
    sys.exit(1)

print("Connecting to Gemini API...")
genai.configure(api_key=api_key)

# Using a fully stable model ID compatible with the legacy client
model = genai.GenerativeModel("gemini-2.5-flash")

prompt = "Confirm connection. Output exactly: 'System online. Environment linked successfully.'"
try:
    response = model.generate_content(prompt)
    print("\n" + response.text.strip())
except Exception as e:
    print(f"\nAPI Error: {e}")
