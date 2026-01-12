from __future__ import annotations

import gradio as gr
from typing import List

from .search import rag, semantic_search, fulltext_search
from .store import ensure_dirs, ingest_file_path, save_upload
from .text_utils import ChunkParams


def build_ui():
    ensure_dirs()

    # Minimal, Oracle Redwood-inspired styling via custom CSS + a soft theme
    css = """
    .gradio-container { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background: #f8f9fb; }
    .gradio-container .prose h1, .gradio-container .prose h2, .gradio-container .prose h3 { color: #1f2937; }
    .gradio-container .prose p { color: #374151; }
    .gr-button { border-radius: 8px; }
    .gr-button.primary { background-color: #2f6db5; border-color: #2f6db5; color: white; }
    .gr-button:hover { filter: brightness(0.95); }
    .gr-input label, .gr-textbox label, .gr-dropdown label, .gr-number label, .gr-slider label { font-weight: 600; color: #1f2937; }
    .gr-box { border-radius: 10px; }
    """

    with gr.Blocks(title="Enterprise Search App", theme=gr.themes.Soft(), css=css) as demo:
        gr.Markdown("# Enterprise Search App\nUpload and search your documents (PDF, HTML, TXT, DOCX). Backed by OCI PostgreSQL + pgvector.")

        with gr.Tab("Upload"):
            files = gr.File(label="Upload files", file_count="multiple", type="filepath")
            status = gr.Textbox(label="Status", lines=4)
            chunk_size = gr.Number(value=1000, precision=0, label="Chunk size (chars)")
            chunk_overlap = gr.Number(value=200, precision=0, label="Chunk overlap (chars)")
            ingest_btn = gr.Button("Ingest")

            def do_ingest(file_list: List[str], chunk_size: int, chunk_overlap: int):
                if not file_list:
                    return "No files provided"
                results = []
                for path in file_list:
                    import os
                    with open(path, "rb") as f:
                        data = f.read()
                    saved = save_upload(data, os.path.basename(path))
                    res = ingest_file_path(saved, chunk_params=ChunkParams(int(chunk_size), int(chunk_overlap)))
                    results.append(f"{os.path.basename(path)}: document_id={res.document_id}, chunks={res.num_chunks}")
                return "\n".join(results)

            ingest_btn.click(do_ingest, inputs=[files, chunk_size, chunk_overlap], outputs=[status])

        with gr.Tab("Search"):
            query = gr.Textbox(label="Query", placeholder="Ask or search...")
            mode = gr.Dropdown(choices=["semantic", "fulltext", "hybrid", "rag"], value="hybrid", label="Mode")
            topk = gr.Slider(minimum=1, maximum=20, value=6, step=1, label="Top K")
            search_btn = gr.Button("Search")
            answer = gr.Textbox(label="Answer / Context", lines=12)
            results = gr.Dataframe(headers=["chunk_id", "document_id", "chunk_index", "distance", "rank", "content"], wrap=True)

            def do_search(q: str, m: str, k: int):
                m = m.lower()
                if m == "semantic":
                    hits = semantic_search(q, top_k=int(k))
                    rows = [[h.chunk_id, h.document_id, h.chunk_index, h.distance, None, h.content] for h in hits]
                    return "\n\n".join(h.content for h in hits), rows
                if m == "fulltext":
                    hits = fulltext_search(q, top_k=int(k))
                    rows = [[h.chunk_id, h.document_id, h.chunk_index, None, h.rank, h.content] for h in hits]
                    return "\n\n".join(h.content for h in hits), rows
                if m == "rag":
                    ans, hits = rag(q, mode="hybrid", top_k=int(k))
                    rows = [[h.chunk_id, h.document_id, h.chunk_index, h.distance, h.rank, h.content] for h in hits]
                    return ans, rows
                hits = rag(q, mode="hybrid", top_k=int(k))[1]
                rows = [[h.chunk_id, h.document_id, h.chunk_index, h.distance, h.rank, h.content] for h in hits]
                return "\n\n".join(h.content for h in hits), rows

            search_btn.click(do_search, inputs=[query, mode, topk], outputs=[answer, results])

    return demo
