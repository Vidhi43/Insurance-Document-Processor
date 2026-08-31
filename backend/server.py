import os, sys, shutil, json, asyncio
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from llm_extractor import process_document, load_env_file

load_env_file()
from db import init_db_pool
app = FastAPI(title="Insurance Document Processor API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup_event():
    init_db_pool()


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"




@app.post("/api/process")
async def process_document_endpoint(file: UploadFile = File(...), extraction_mode: str = Form("both")):
    valid = [".pdf",".png",".jpg",".jpeg",".docx",".doc",".mp3",".wav",".m4a",".aac",".flac",".mp4",".mov",".avi",".mkv",".webm"]
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in valid:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")

    uploads_dir = os.path.join(os.getcwd(), "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    tmp = os.path.join(uploads_dir, f"upload_{os.urandom(8).hex()}{ext}")

    async def stream():
        try:
            # Step 1 — save file
            yield sse("progress", {"step": 1, "message": "Saving uploaded file..."})
            await asyncio.sleep(0)
            with open(tmp, "wb") as buf:
                shutil.copyfileobj(file.file, buf)

            api_key = os.environ.get("OPENROUTER_API_KEY")
            model   = os.environ.get("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free"
            if not api_key:
                yield sse("error", {"message": "OPENROUTER_API_KEY not configured."}); return

            # Step 2 — OCR + cleaning (blocking -> executor)
            yield sse("progress", {"step": 2, "message": "Running OCR & layout-aware extraction..."})
            await asyncio.sleep(0)

            # Step 3/4/5 — classify -> extract -> validate, all inside the
            # orchestrator. We can't stream sub-steps from inside a single
            # blocking executor call, so steps 3-5 are reported as one
            # combined progress event before the call, and "done"/"error"
            # carries the real result after.
            yield sse("progress", {"step": 3, "message": "Classifying document type, then extracting and validating fields..."})
            await asyncio.sleep(0)

            loop = asyncio.get_event_loop()
            doc_type, validated = await loop.run_in_executor(
                None, lambda: process_document(tmp, api_key, model, extraction_mode=extraction_mode)
            )

            if validated is None:
                yield sse("error", {"message": f"Extraction or validation failed for doc_type='{doc_type}'."})
                return

            yield sse("done", {
                "doc_type": doc_type,
                "result": json.loads(validated.model_dump_json(by_alias=True)),
            })

        except Exception as e:
            yield sse("error", {"message": str(e)})
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except: pass

    return StreamingResponse(stream(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
