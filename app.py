"""
Manga OCR Search Tool
---------------------
Index manga panels with Japanese OCR and search by text.

Requirements:
    pip install manga-ocr gradio pillow pysqlite3

Usage:
    python app.py
"""

import os
import sqlite3
import json
import base64
from pathlib import Path
from io import BytesIO

import gradio as gr
from PIL import Image

# ── Database ──────────────────────────────────────────────────────────────────

DB_PATH = "manga_index.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS panels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath    TEXT    NOT NULL UNIQUE,
                filename    TEXT    NOT NULL,
                folder      TEXT    NOT NULL,
                ocr_text    TEXT    NOT NULL DEFAULT '',
                indexed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Full-text search virtual table
            CREATE VIRTUAL TABLE IF NOT EXISTS panels_fts
            USING fts5(
                ocr_text,
                content='panels',
                content_rowid='id',
                tokenize='unicode61'
            );

            -- Keep FTS in sync
            CREATE TRIGGER IF NOT EXISTS panels_ai AFTER INSERT ON panels BEGIN
                INSERT INTO panels_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
            END;
            CREATE TRIGGER IF NOT EXISTS panels_ad AFTER DELETE ON panels BEGIN
                INSERT INTO panels_fts(panels_fts, rowid, ocr_text)
                VALUES ('delete', old.id, old.ocr_text);
            END;
            CREATE TRIGGER IF NOT EXISTS panels_au AFTER UPDATE ON panels BEGIN
                INSERT INTO panels_fts(panels_fts, rowid, ocr_text)
                VALUES ('delete', old.id, old.ocr_text);
                INSERT INTO panels_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
            END;
        """)

# ── OCR ───────────────────────────────────────────────────────────────────────

_ocr_model = None

def get_ocr():
    """Lazy-load manga-ocr model (downloads ~400 MB on first run)."""
    global _ocr_model
    if _ocr_model is None:
        from manga_ocr import MangaOcr
        _ocr_model = MangaOcr()
    return _ocr_model

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

def index_folder(folder_path: str, progress=gr.Progress()):
    """
    OCR every image in folder_path and upsert results into the DB.
    Returns a status string.
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        return f"❌ Not a valid directory: {folder_path}"

    images = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED_EXTS]
    if not images:
        return f"⚠️ No images found in {folder_path}"

    ocr = get_ocr()
    new_count = updated_count = skipped_count = error_count = 0

    with get_db() as conn:
        existing = {
            row["filepath"] for row in conn.execute("SELECT filepath FROM panels")
        }

    for i, img_path in enumerate(progress.tqdm(images, desc="OCR indexing")):
        filepath_str = str(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
            text = ocr(img).strip()

            with get_db() as conn:
                if filepath_str in existing:
                    conn.execute(
                        "UPDATE panels SET ocr_text=?, indexed_at=datetime('now') WHERE filepath=?",
                        (text, filepath_str)
                    )
                    updated_count += 1
                else:
                    conn.execute(
                        "INSERT INTO panels (filepath, filename, folder, ocr_text) VALUES (?,?,?,?)",
                        (filepath_str, img_path.name, str(img_path.parent), text)
                    )
                    new_count += 1
        except Exception as e:
            error_count += 1
            print(f"Error on {img_path}: {e}")

    total = len(images)
    return (
        f"✅ Done! {total} images processed.\n"
        f"  • {new_count} newly indexed\n"
        f"  • {updated_count} updated\n"
        f"  • {error_count} errors"
    )

# ── Search ────────────────────────────────────────────────────────────────────

def search_panels(query: str, max_results: int = 20):
    """
    Search the FTS index for query. Returns list of (image, caption) tuples
    for Gradio Gallery.
    """
    if not query.strip():
        return [], "Please enter a search term."

    with get_db() as conn:
        # Try FTS first
        rows = conn.execute(
            """
            SELECT p.filepath, p.filename, p.folder, p.ocr_text
            FROM panels p
            JOIN panels_fts fts ON fts.rowid = p.id
            WHERE panels_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, max_results)
        ).fetchall()

        # Fallback: LIKE search (catches partial kana/kanji missed by FTS tokeniser)
        if not rows:
            rows = conn.execute(
                """
                SELECT filepath, filename, folder, ocr_text
                FROM panels
                WHERE ocr_text LIKE ?
                LIMIT ?
                """,
                (f"%{query}%", max_results)
            ).fetchall()

    if not rows:
        return [], f"No results found for「{query}」"

    gallery_items = []
    for row in rows:
        try:
            img = Image.open(row["filepath"])
            # Caption shows matched text with query highlighted (plain text)
            caption = f"{row['filename']}\n{row['ocr_text']}"
            gallery_items.append((img, caption))
        except Exception:
            pass

    status = f"Found {len(gallery_items)} panel(s) for「{query}」"
    return gallery_items, status

# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        folders = conn.execute("SELECT COUNT(DISTINCT folder) FROM panels").fetchone()[0]
        recent = conn.execute(
            "SELECT filename, ocr_text, indexed_at FROM panels ORDER BY indexed_at DESC LIMIT 5"
        ).fetchall()

    lines = [f"📚 **{total}** panels indexed across **{folders}** folder(s)\n"]
    if recent:
        lines.append("**Recently indexed:**")
        for r in recent:
            preview = r["ocr_text"][:40] + "…" if len(r["ocr_text"]) > 40 else r["ocr_text"]
            lines.append(f"- `{r['filename']}` — {preview}")
    return "\n".join(lines)

def clear_index():
    with get_db() as conn:
        conn.executescript("""
            DELETE FROM panels;
            DELETE FROM panels_fts;
        """)
    return "🗑️ Index cleared."

# ── Gradio UI ─────────────────────────────────────────────────────────────────

def build_ui():
    with gr.Blocks(
        title="Manga OCR Search",
        theme=gr.themes.Base(
            primary_hue="rose",
            neutral_hue="zinc",
            font=gr.themes.GoogleFont("Noto Sans JP"),
        ),
        css="""
            .gradio-container { max-width: 1100px; margin: auto; }
            #title { text-align: center; padding: 1rem 0 0.5rem; }
            #title h1 { font-size: 2rem; font-weight: 700; }
            #title p  { color: #888; font-size: 0.95rem; }
            .panel-gallery .thumbnail-item { border-radius: 6px; }
        """
    ) as demo:

        gr.HTML("""
            <div id="title">
                <h1>📖 Manga OCR Search</h1>
                <p>Index manga panels with Japanese OCR, then search by word or phrase.</p>
            </div>
        """)

        with gr.Tabs():

            # ── Tab 1: Index ──────────────────────────────────────────────
            with gr.Tab("📥 Index Panels"):
                gr.Markdown(
                    "Point to a folder of manga panel images. "
                    "OCR runs locally via **manga-ocr** — first run downloads ~400 MB."
                )
                with gr.Row():
                    folder_input = gr.Textbox(
                        label="Panel Folder Path",
                        placeholder="/path/to/gochiusa/panels",
                        scale=4,
                    )
                    index_btn = gr.Button("▶ Index", variant="primary", scale=1)

                index_status = gr.Textbox(label="Status", interactive=False, lines=4)
                index_btn.click(fn=index_folder, inputs=folder_input, outputs=index_status)

            # ── Tab 2: Search ─────────────────────────────────────────────
            with gr.Tab("🔍 Search"):
                with gr.Row():
                    search_input = gr.Textbox(
                        label="Search (Japanese)",
                        placeholder="コーヒー",
                        scale=4,
                    )
                    max_results = gr.Slider(
                        label="Max results", minimum=5, maximum=100, step=5, value=20, scale=1
                    )
                    search_btn = gr.Button("Search", variant="primary", scale=1)

                search_status = gr.Textbox(label="", interactive=False, lines=1)
                gallery = gr.Gallery(
                    label="Matching Panels",
                    elem_classes=["panel-gallery"],
                    columns=3,
                    height=600,
                    object_fit="contain",
                )

                search_btn.click(
                    fn=search_panels,
                    inputs=[search_input, max_results],
                    outputs=[gallery, search_status],
                )
                search_input.submit(
                    fn=search_panels,
                    inputs=[search_input, max_results],
                    outputs=[gallery, search_status],
                )

            # ── Tab 3: Stats / Manage ─────────────────────────────────────
            with gr.Tab("📊 Stats & Manage"):
                refresh_btn = gr.Button("Refresh Stats")
                stats_out = gr.Markdown()
                refresh_btn.click(fn=get_stats, outputs=stats_out)

                gr.Markdown("---")
                gr.Markdown("⚠️ **Danger zone**")
                clear_btn = gr.Button("🗑️ Clear Entire Index", variant="stop")
                clear_status = gr.Textbox(interactive=False, lines=1)
                clear_btn.click(fn=clear_index, outputs=clear_status)

        demo.load(fn=get_stats, outputs=stats_out)

    return demo


if __name__ == "__main__":
    init_db()
    ui = build_ui()
    ui.launch(inbrowser=True)
