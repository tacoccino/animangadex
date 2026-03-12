"""
Manga OCR + Anime Subtitle Search Tool
---------------------------------------
• Manga:  Index panel images with Japanese OCR, search by word/phrase
• Anime:  Index .srt/.ass subtitle files, search and jump to scene in mpv

Requirements:
    pip install manga-ocr gradio pillow pysubs2

Usage:
    python app.py
"""

import os
import re
import sqlite3
import subprocess
from pathlib import Path

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
            -- ── Manga panels ──────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS panels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath    TEXT    NOT NULL UNIQUE,
                filename    TEXT    NOT NULL,
                folder      TEXT    NOT NULL,
                ocr_text    TEXT    NOT NULL DEFAULT '',
                indexed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS panels_fts
            USING fts5(
                ocr_text,
                content='panels',
                content_rowid='id',
                tokenize='unicode61'
            );

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

            -- ── Anime subtitles ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS subtitles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_filepath TEXT    NOT NULL,
                vid_filepath TEXT,                      -- matched video file, may be NULL
                episode      TEXT    NOT NULL,          -- display name (stem of sub file)
                start_ms     INTEGER NOT NULL,          -- subtitle start in milliseconds
                end_ms       INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                indexed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS subtitles_fts
            USING fts5(
                text,
                content='subtitles',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS subs_ai AFTER INSERT ON subtitles BEGIN
                INSERT INTO subtitles_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS subs_ad AFTER DELETE ON subtitles BEGIN
                INSERT INTO subtitles_fts(subtitles_fts, rowid, text)
                VALUES ('delete', old.id, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS subs_au AFTER UPDATE ON subtitles BEGIN
                INSERT INTO subtitles_fts(subtitles_fts, rowid, text)
                VALUES ('delete', old.id, old.text);
                INSERT INTO subtitles_fts(rowid, text) VALUES (new.id, new.text);
            END;
        """)

# ── OCR (manga) ───────────────────────────────────────────────────────────────

_ocr_model = None

def get_ocr():
    global _ocr_model
    if _ocr_model is None:
        from manga_ocr import MangaOcr
        _ocr_model = MangaOcr()
    return _ocr_model

SUPPORTED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

def index_folder(folder_path: str, progress=gr.Progress()):
    folder = Path(folder_path)
    if not folder.is_dir():
        return f"❌ Not a valid directory: {folder_path}"

    images = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED_IMG_EXTS]
    if not images:
        return f"⚠️ No images found in {folder_path}"

    ocr = get_ocr()
    new_count = updated_count = skipped_count = error_count = 0

    with get_db() as conn:
        existing = {row["filepath"] for row in conn.execute("SELECT filepath FROM panels")}

    for img_path in progress.tqdm(images, desc="OCR indexing"):
        filepath_str = str(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
            text = ocr(img).strip()
            if not text:
                skipped_count += 1
                continue
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

    return (
        f"✅ Done! {len(images)} images scanned.\n"
        f"  • {new_count} newly indexed\n"
        f"  • {updated_count} updated\n"
        f"  • {skipped_count} skipped (no text detected)\n"
        f"  • {error_count} errors"
    )

def search_panels(query: str, max_results: int = 20):
    if not query.strip():
        return [], "Please enter a search term."

    with get_db() as conn:
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

        if not rows:
            rows = conn.execute(
                "SELECT filepath, filename, folder, ocr_text FROM panels WHERE ocr_text LIKE ? LIMIT ?",
                (f"%{query}%", max_results)
            ).fetchall()

    if not rows:
        return [], f"No results found for「{query}」"

    gallery_items = []
    for row in rows:
        try:
            img = Image.open(row["filepath"])
            caption = f"{row['filename']}\n{row['ocr_text']}"
            gallery_items.append((img, caption))
        except Exception:
            pass

    return gallery_items, f"Found {len(gallery_items)} panel(s) for「{query}」"

# ── Subtitle indexing (anime) ─────────────────────────────────────────────────

SUPPORTED_SUB_EXTS = {".srt", ".ass", ".ssa"}
SUPPORTED_VID_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".mov"}

def _strip_ass_tags(text: str) -> str:
    """Remove ASS/SSA override tags like {\\an8}, {\\i1}, etc."""
    return re.sub(r"\{[^}]*\}", "", text).strip()

def _find_video(sub_path: Path) -> str | None:
    """
    Try to find a video file in the same directory with the same stem.
    Handles language suffixes: S01E01.ja.srt → S01E01.mkv
    """
    stem = sub_path.stem
    stem = re.sub(r"\.(ja|jpn|jp)$", "", stem, flags=re.IGNORECASE)
    for ext in SUPPORTED_VID_EXTS:
        candidate = sub_path.parent / (stem + ext)
        if candidate.exists():
            return str(candidate)
    return None

def index_subtitles(folder_path: str, progress=gr.Progress()):
    try:
        import pysubs2
    except ImportError:
        return "❌ pysubs2 not installed. Run: pip install pysubs2"

    folder = Path(folder_path)
    if not folder.is_dir():
        return f"❌ Not a valid directory: {folder_path}"

    sub_files = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED_SUB_EXTS]
    if not sub_files:
        return f"⚠️ No subtitle files (.srt/.ass/.ssa) found in {folder_path}"

    total_lines = file_count = error_count = 0

    for sub_path in progress.tqdm(sub_files, desc="Indexing subtitles"):
        try:
            subs    = pysubs2.load(str(sub_path), encoding="utf-8")
            vid     = _find_video(sub_path)
            episode = sub_path.stem

            rows = []
            for line in subs:
                text = _strip_ass_tags(line.text).replace("\\N", " ").replace("\\n", " ").strip()
                if not text:
                    continue
                rows.append((str(sub_path), vid, episode, line.start, line.end, text))

            with get_db() as conn:
                conn.execute("DELETE FROM subtitles WHERE sub_filepath=?", (str(sub_path),))
                conn.executemany(
                    "INSERT INTO subtitles (sub_filepath, vid_filepath, episode, start_ms, end_ms, text) "
                    "VALUES (?,?,?,?,?,?)",
                    rows
                )

            total_lines += len(rows)
            file_count  += 1

        except Exception as e:
            error_count += 1
            print(f"Error on {sub_path}: {e}")

    return (
        f"✅ Done! {file_count} subtitle file(s) indexed.\n"
        f"  • {total_lines} lines indexed\n"
        f"  • {error_count} errors"
    )

# ── Subtitle search ───────────────────────────────────────────────────────────

def ms_to_timestamp(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem,    60_000)
    s, ms_ = divmod(rem,     1_000)
    return f"{h}:{m:02d}:{s:02d}.{ms_//100}"

def ms_to_mpv_start(ms: int) -> str:
    """Seek 1 second before the line starts, for context."""
    return f"{max(0, (ms - 1000) / 1000):.1f}"

def search_subtitles(query: str, max_results: int = 30):
    if not query.strip():
        return [], "Please enter a search term."

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.episode, s.start_ms, s.end_ms, s.text,
                   s.vid_filepath, s.sub_filepath
            FROM subtitles s
            JOIN subtitles_fts fts ON fts.rowid = s.id
            WHERE subtitles_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, max_results)
        ).fetchall()

        if not rows:
            rows = conn.execute(
                """
                SELECT id, episode, start_ms, end_ms, text, vid_filepath, sub_filepath
                FROM subtitles WHERE text LIKE ? LIMIT ?
                """,
                (f"%{query}%", max_results)
            ).fetchall()

    if not rows:
        return [], f"No results for「{query}」"

    table = []
    for r in rows:
        has_video = "▶ Open" if r["vid_filepath"] else "—"
        table.append([r["id"], r["episode"], ms_to_timestamp(r["start_ms"]), r["text"], has_video])

    return table, f"Found {len(table)} line(s) for「{query}」"

def open_in_mpv(selected: gr.SelectData, table_data):
    if selected is None or table_data is None:
        return "Select a result row to open in mpv."
    try:
        import pandas as pd
        row_idx = selected.index[0]
        if isinstance(table_data, pd.DataFrame):
            row_id = int(table_data.iloc[row_idx, 0])
        else:
            row_id = int(table_data[row_idx][0])
    except Exception:
        return "Couldn't determine selected row."

    with get_db() as conn:
        row = conn.execute(
            "SELECT vid_filepath, start_ms FROM subtitles WHERE id=?", (row_id,)
        ).fetchone()

    if not row or not row["vid_filepath"]:
        return (
            "⚠️ No video file found. Make sure the video is in the same folder as the "
            "subtitle file and shares the same filename (e.g. S01E01.mkv + S01E01.ja.srt)."
        )

    vid   = row["vid_filepath"]
    start = ms_to_mpv_start(row["start_ms"])

    # Common Windows install paths in addition to whatever is on PATH
    mpv_candidates = [
        "mpv",
        r"C:\Program Files\mpv\mpv.exe",
        r"C:\Program Files (x86)\mpv\mpv.exe",
        str(Path.home() / "scoop" / "shims" / "mpv.exe"),
        str(Path.home() / "AppData" / "Local" / "Programs" / "mpv" / "mpv.exe"),
    ]
    vlc_candidates = [
        "vlc",
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ]

    for mpv in mpv_candidates:
        if mpv != "mpv" and not Path(mpv).exists():
            continue
        try:
            subprocess.Popen([mpv, f"--start={start}", vid])
            return f"▶ Opened mpv at {ms_to_timestamp(row['start_ms'])}  —  {Path(vid).name}"
        except FileNotFoundError:
            continue

    for vlc in vlc_candidates:
        if vlc != "vlc" and not Path(vlc).exists():
            continue
        try:
            subprocess.Popen([vlc, f"--start-time={float(start):.0f}", vid])
            return f"▶ Opened VLC at {ms_to_timestamp(row['start_ms'])}  —  {Path(vid).name}"
        except FileNotFoundError:
            continue

    return (
        "❌ Neither mpv nor VLC found. Make sure one is installed and either:\n"
        "  • Added to your system PATH, or\n"
        "  • Installed in a standard location (Program Files, Scoop, etc.)"
    )

# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats():
    with get_db() as conn:
        panel_count   = conn.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        panel_folders = conn.execute("SELECT COUNT(DISTINCT folder) FROM panels").fetchone()[0]
        sub_lines     = conn.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0]
        sub_episodes  = conn.execute("SELECT COUNT(DISTINCT episode) FROM subtitles").fetchone()[0]

    return (
        f"### 📖 Manga\n"
        f"**{panel_count}** panels across **{panel_folders}** folder(s)\n\n"
        f"### 🎬 Anime\n"
        f"**{sub_lines}** subtitle lines across **{sub_episodes}** episode(s)"
    )

def clear_manga_index():
    with get_db() as conn:
        conn.executescript("DELETE FROM panels; DELETE FROM panels_fts;")
    return "🗑️ Manga index cleared."

def clear_anime_index():
    with get_db() as conn:
        conn.executescript("DELETE FROM subtitles; DELETE FROM subtitles_fts;")
    return "🗑️ Anime index cleared."

# ── Gradio UI ─────────────────────────────────────────────────────────────────

def build_ui():
    with gr.Blocks(title="Manga & Anime Search") as demo:

        gr.HTML("""
            <div id="title">
                <h1>📖🎬 Manga &amp; Anime Search</h1>
                <p>Search Japanese text across manga panels and anime subtitles.</p>
            </div>
        """)

        with gr.Tabs():

            # ── Tab 1: Index ──────────────────────────────────────────────
            with gr.Tab("📥 Index"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### 📖 Manga Panels")
                        gr.Markdown("Point to a folder of panel images. OCR runs locally via **manga-ocr** — first run downloads ~400 MB.")
                        manga_folder = gr.Textbox(label="Panel Folder Path", placeholder="/path/to/gochiusa/panels")
                        manga_index_btn = gr.Button("▶ Index Panels", variant="primary")
                        manga_index_status = gr.Textbox(label="Status", interactive=False, lines=5)
                        manga_index_btn.click(fn=index_folder, inputs=manga_folder, outputs=manga_index_status)

                    with gr.Column():
                        gr.Markdown("### 🎬 Anime Subtitles")
                        gr.Markdown(
                            "Point to a folder containing `.srt` or `.ass` subtitle files. "
                            "Video files should be in the **same folder** with the **same filename** stem."
                        )
                        anime_folder = gr.Textbox(label="Subtitle/Video Folder Path", placeholder="/path/to/gochiusa/episodes")
                        anime_index_btn = gr.Button("▶ Index Subtitles", variant="primary")
                        anime_index_status = gr.Textbox(label="Status", interactive=False, lines=5)
                        anime_index_btn.click(fn=index_subtitles, inputs=anime_folder, outputs=anime_index_status)

            # ── Tab 2: Manga Search ───────────────────────────────────────
            with gr.Tab("🔍 Manga Search"):
                with gr.Row():
                    manga_search_input = gr.Textbox(label="Search (Japanese)", placeholder="コーヒー", scale=4)
                    manga_max = gr.Slider(label="Max results", minimum=5, maximum=100, step=5, value=20, scale=1)
                    manga_search_btn = gr.Button("Search", variant="primary", scale=1)

                manga_status = gr.Textbox(label="", interactive=False, lines=1)
                gallery = gr.Gallery(
                    label="Matching Panels",
                    elem_classes=["panel-gallery"],
                    columns=3, height=620, object_fit="contain",
                )
                manga_search_btn.click(fn=search_panels, inputs=[manga_search_input, manga_max], outputs=[gallery, manga_status])
                manga_search_input.submit(fn=search_panels, inputs=[manga_search_input, manga_max], outputs=[gallery, manga_status])

            # ── Tab 3: Anime Search ───────────────────────────────────────
            with gr.Tab("🎬 Anime Search"):
                with gr.Row():
                    anime_search_input = gr.Textbox(label="Search (Japanese)", placeholder="コーヒー", scale=4)
                    anime_max = gr.Slider(label="Max results", minimum=5, maximum=100, step=5, value=30, scale=1)
                    anime_search_btn = gr.Button("Search", variant="primary", scale=1)

                anime_status = gr.Textbox(label="", interactive=False, lines=1)
                mpv_status   = gr.Textbox(label="Player", interactive=False, lines=1)

                results_table = gr.Dataframe(
                    headers=["id", "Episode", "Timestamp", "Line", "Video"],
                    datatype=["number", "str", "str", "str", "str"],
                    column_count=(5, "fixed"),
                    interactive=False,
                    wrap=True,
                )
                gr.Markdown("*Click any row to open that scene in mpv (or VLC as fallback).*")

                anime_search_btn.click(fn=search_subtitles, inputs=[anime_search_input, anime_max], outputs=[results_table, anime_status])
                anime_search_input.submit(fn=search_subtitles, inputs=[anime_search_input, anime_max], outputs=[results_table, anime_status])
                results_table.select(fn=open_in_mpv, inputs=[results_table], outputs=[mpv_status])

            # ── Tab 4: Stats / Manage ─────────────────────────────────────
            with gr.Tab("📊 Stats & Manage"):
                refresh_btn = gr.Button("Refresh Stats")
                stats_out   = gr.Markdown()
                refresh_btn.click(fn=get_stats, outputs=stats_out)

                gr.Markdown("---")
                gr.Markdown("⚠️ **Danger zone**")
                with gr.Row():
                    clear_manga_btn = gr.Button("🗑️ Clear Manga Index", variant="stop")
                    clear_anime_btn = gr.Button("🗑️ Clear Anime Index", variant="stop")
                clear_status = gr.Textbox(interactive=False, lines=1)
                clear_manga_btn.click(fn=clear_manga_index, outputs=clear_status)
                clear_anime_btn.click(fn=clear_anime_index, outputs=clear_status)

        demo.load(fn=get_stats, outputs=stats_out)

    return demo


if __name__ == "__main__":
    init_db()
    ui = build_ui()
    ui.launch(
        inbrowser=True,
        theme=gr.themes.Base(
            primary_hue="rose",
            neutral_hue="zinc",
            font=gr.themes.GoogleFont("Noto Sans JP"),
        ),
        css="""
            .gradio-container { max-width: 1200px; margin: auto; }
            #title { text-align: center; padding: 1rem 0 0.5rem; }
            #title h1 { font-size: 2rem; font-weight: 700; }
            #title p  { color: #888; font-size: 0.95rem; }
            .panel-gallery .thumbnail-item { border-radius: 6px; }
        """,
    )
