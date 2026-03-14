"""
Animangadex
-----------
Search Japanese text across manga panels and anime subtitles.

Requirements:
    pip install manga-ocr gradio pillow pysubs2

Usage:
    python app.py
"""

import re
import sqlite3
import subprocess
from pathlib import Path

import gradio as gr
from PIL import Image


import json
import platform

SETTINGS_PATH = "settings.json"

DEFAULT_PLAYER_PATHS = {
    "mpv": {
        "Darwin":  "/usr/local/bin/mpv",
        "Linux":   "/usr/bin/mpv",
        "Windows": r"C:\Program Files\mpv\mpv.exe",
    },
    "vlc": {
        "Darwin":  "/Applications/VLC.app/Contents/MacOS/VLC",
        "Linux":   "/usr/bin/vlc",
        "Windows": r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    },
}

def default_settings():
    os_name = platform.system()
    return {
        "playback_mode": "browser",          # "browser" | "mpv" | "vlc"
        "mpv_path": DEFAULT_PLAYER_PATHS["mpv"].get(os_name, "mpv"),
        "vlc_path": DEFAULT_PLAYER_PATHS["vlc"].get(os_name, "vlc"),
    }

def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Fill in any missing keys from defaults
        defaults = default_settings()
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return default_settings()

def save_settings(playback_mode, mpv_path, vlc_path):
    settings = {
        "playback_mode": playback_mode,
        "mpv_path": mpv_path,
        "vlc_path": vlc_path,
    }
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    return "Settings saved."

# Database ---------------------------------------------------------------

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
            CREATE VIRTUAL TABLE IF NOT EXISTS panels_fts
            USING fts5(ocr_text, content='panels', content_rowid='id', tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS panels_ai AFTER INSERT ON panels BEGIN
                INSERT INTO panels_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
            END;
            CREATE TRIGGER IF NOT EXISTS panels_ad AFTER DELETE ON panels BEGIN
                INSERT INTO panels_fts(panels_fts, rowid, ocr_text) VALUES ('delete', old.id, old.ocr_text);
            END;
            CREATE TRIGGER IF NOT EXISTS panels_au AFTER UPDATE ON panels BEGIN
                INSERT INTO panels_fts(panels_fts, rowid, ocr_text) VALUES ('delete', old.id, old.ocr_text);
                INSERT INTO panels_fts(rowid, ocr_text) VALUES (new.id, new.ocr_text);
            END;

            CREATE TABLE IF NOT EXISTS subtitles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_filepath TEXT    NOT NULL,
                vid_filepath TEXT,
                episode      TEXT    NOT NULL,
                start_ms     INTEGER NOT NULL,
                end_ms       INTEGER NOT NULL,
                text         TEXT    NOT NULL,
                indexed_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS subtitles_fts
            USING fts5(text, content='subtitles', content_rowid='id', tokenize='unicode61');
            CREATE TRIGGER IF NOT EXISTS subs_ai AFTER INSERT ON subtitles BEGIN
                INSERT INTO subtitles_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS subs_ad AFTER DELETE ON subtitles BEGIN
                INSERT INTO subtitles_fts(subtitles_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS subs_au AFTER UPDATE ON subtitles BEGIN
                INSERT INTO subtitles_fts(subtitles_fts, rowid, text) VALUES ('delete', old.id, old.text);
                INSERT INTO subtitles_fts(rowid, text) VALUES (new.id, new.text);
            END;
        """)

# OCR (manga) ------------------------------------------------------------

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
        return f"Not a valid directory: {folder_path}"
    images = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED_IMG_EXTS]
    if not images:
        return f"No images found in {folder_path}"

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
        f"Done! {len(images)} images scanned.\n"
        f"  {new_count} newly indexed\n"
        f"  {updated_count} updated\n"
        f"  {skipped_count} skipped (no text detected)\n"
        f"  {error_count} errors"
    )

def search_panels(query: str, max_results: int = 20):
    if not query.strip():
        return [], "Please enter a search term."
    with get_db() as conn:
        rows = conn.execute(
            """SELECT p.filepath, p.filename, p.ocr_text
               FROM panels p JOIN panels_fts fts ON fts.rowid = p.id
               WHERE panels_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, max_results)
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT filepath, filename, ocr_text FROM panels WHERE ocr_text LIKE ? LIMIT ?",
                (f"%{query}%", max_results)
            ).fetchall()

    if not rows:
        return [], f"No results found for {query}"

    gallery_items = []
    for row in rows:
        try:
            img = Image.open(row["filepath"])
            caption = f"{row['filename']}\n{row['ocr_text']}"
            gallery_items.append((img, caption))
        except Exception:
            pass
    return gallery_items, f"Found {len(gallery_items)} panel(s) for {query}"

# Subtitle indexing (anime) ----------------------------------------------

SUPPORTED_SUB_EXTS = {".srt", ".ass", ".ssa"}
SUPPORTED_VID_EXTS = {".mkv", ".mp4", ".avi", ".webm", ".mov"}

def _strip_ass_tags(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "", text).strip()

def _find_video(sub_path: Path):
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
        return "pysubs2 not installed. Run: pip install pysubs2"

    folder = Path(folder_path)
    if not folder.is_dir():
        return f"Not a valid directory: {folder_path}"

    sub_files = [p for p in sorted(folder.rglob("*")) if p.suffix.lower() in SUPPORTED_SUB_EXTS]
    if not sub_files:
        return f"No subtitle files (.srt/.ass/.ssa) found in {folder_path}"

    total_lines = file_count = error_count = 0
    for sub_path in progress.tqdm(sub_files, desc="Indexing subtitles"):
        try:
            subs = pysubs2.load(str(sub_path), encoding="utf-8")
            vid = _find_video(sub_path)
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
                    "INSERT INTO subtitles (sub_filepath, vid_filepath, episode, start_ms, end_ms, text) VALUES (?,?,?,?,?,?)",
                    rows
                )
            total_lines += len(rows)
            file_count += 1
        except Exception as e:
            error_count += 1
            print(f"Error on {sub_path}: {e}")

    return (
        f"Done! {file_count} subtitle file(s) indexed.\n"
        f"  {total_lines} lines indexed\n"
        f"  {error_count} errors"
    )

# Subtitle search --------------------------------------------------------

def ms_to_timestamp(ms: int) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms_ = divmod(rem, 1_000)
    return f"{h}:{m:02d}:{s:02d}.{ms_//100}"

def search_subtitles(query: str, max_results: int = 30):
    if not query.strip():
        return [], "Please enter a search term."
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.episode, s.start_ms, s.text, s.vid_filepath
               FROM subtitles s JOIN subtitles_fts fts ON fts.rowid = s.id
               WHERE subtitles_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, max_results)
        ).fetchall()
        if not rows:
            rows = conn.execute(
                "SELECT id, episode, start_ms, text, vid_filepath FROM subtitles WHERE text LIKE ? LIMIT ?",
                (f"%{query}%", max_results)
            ).fetchall()

    if not rows:
        return [], f"No results for {query}"

    table = []
    for r in rows:
        # Encode missing-video flag into id field as negative (used by CSS to gray the row)
        row_id = r["id"] if r["vid_filepath"] else -r["id"]
        table.append([row_id, r["episode"], ms_to_timestamp(r["start_ms"]), r["text"]])
    return table, f"Found {len(table)} line(s) for {query}"

def _sub_to_vtt(sub_filepath: str) -> str:
    """Convert a .srt or .ass subtitle file to WebVTT format string for browser playback."""
    try:
        import pysubs2
        subs = pysubs2.load(sub_filepath, encoding="utf-8")
        lines = ["WEBVTT", ""]
        for i, line in enumerate(subs):
            text = _strip_ass_tags(line.text).replace("\\N", "\n").replace("\\n", "\n").strip()
            if not text:
                continue
            def ms_to_vtt(ms):
                h, r = divmod(ms, 3_600_000)
                m, r = divmod(r, 60_000)
                s, ms_ = divmod(r, 1_000)
                return f"{h:02d}:{m:02d}:{s:02d}.{ms_:03d}"
            lines.append(f"{i+1}")
            lines.append(f"{ms_to_vtt(line.start)} --> {ms_to_vtt(line.end)}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        print(f"VTT conversion error: {e}")
        return ""

def load_scene(selected: gr.SelectData, table_data, playback_mode_state):
    """On row click: load the video into the in-browser player at the right timestamp."""
    if selected is None or table_data is None:
        return None, 0.0, "Select a result row to load the scene.", "", ""
    try:
        import pandas as pd
        row_idx = selected.index[0]
        row_id = abs(int(table_data.iloc[row_idx, 0] if isinstance(table_data, pd.DataFrame) else table_data[row_idx][0]))
    except Exception:
        return None, 0.0, "Couldn't determine selected row.", "", ""

    with get_db() as conn:
        row = conn.execute(
            "SELECT vid_filepath, sub_filepath, start_ms FROM subtitles WHERE id=?", (row_id,)
        ).fetchone()

    if not row or not row["vid_filepath"]:
        return None, 0.0, (
            "No video file found. Make sure the video is in the same folder as the "
            "subtitle file with the same filename stem (e.g. S01E01.mkv + S01E01.ja.srt)."
        ), "", "no_video"
    if not Path(row["vid_filepath"]).exists():
        return None, 0.0, f"Video file not found at: {row['vid_filepath']}", "", "no_video"

    start_secs = max(0.0, (row["start_ms"] - 1000) / 1000)
    label = f"{Path(row['vid_filepath']).name}  at  {ms_to_timestamp(row['start_ms'])}"
    vtt = _sub_to_vtt(row["sub_filepath"]) if row["sub_filepath"] else ""
    settings = load_settings()
    mode = playback_mode_state if playback_mode_state else settings["playback_mode"]

    if mode in ("mpv", "vlc"):
        # Launch external player and return nothing to the browser player
        player_path = settings["mpv_path"] if mode == "mpv" else settings["vlc_path"]
        start_arg = f"--start={start_secs:.1f}" if mode == "mpv" else f"--start-time={start_secs:.0f}"
        try:
            subprocess.Popen([player_path, start_arg, row["vid_filepath"]])
            return None, 0.0, label, "", "external_ok"
        except FileNotFoundError:
            return None, 0.0, f"Player not found at: {player_path}. Check your path in Settings.", "", "external_err"

    return row["vid_filepath"], start_secs, label, vtt, "browser"

# Stats ------------------------------------------------------------------

def get_stats():
    with get_db() as conn:
        panel_count   = conn.execute("SELECT COUNT(*) FROM panels").fetchone()[0]
        panel_folders = conn.execute("SELECT COUNT(DISTINCT folder) FROM panels").fetchone()[0]
        sub_lines     = conn.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0]
        sub_episodes  = conn.execute("SELECT COUNT(DISTINCT episode) FROM subtitles").fetchone()[0]
    return (
        f"### Manga\n**{panel_count}** panels across **{panel_folders}** folder(s)\n\n"
        f"### Anime\n**{sub_lines}** subtitle lines across **{sub_episodes}** episode(s)"
    )

def clear_manga_index():
    with get_db() as conn:
        conn.executescript("DELETE FROM panels; DELETE FROM panels_fts;")
    return "Manga index cleared."

def get_allowed_paths():
    """Collect all unique folders containing indexed files for Gradio allowed_paths."""
    paths = set()
    with get_db() as conn:
        for row in conn.execute("SELECT DISTINCT vid_filepath FROM subtitles WHERE vid_filepath IS NOT NULL"):
            paths.add(str(Path(row["vid_filepath"]).parent))
        for row in conn.execute("SELECT DISTINCT folder FROM panels"):
            paths.add(row["folder"])
    return list(paths)

def clear_anime_index():
    with get_db() as conn:
        conn.executescript("DELETE FROM subtitles; DELETE FROM subtitles_fts;")
    return "Anime index cleared."

# UI ---------------------------------------------------------------------

TABLE_JS = """
() => {
    // Gray out rows whose hidden id cell contains a negative number (no video matched)
    setTimeout(() => {
        const table = document.querySelector('#anime-results table');
        if (!table) return;
        table.querySelectorAll('tbody tr').forEach(row => {
            // Gray out rows with missing video
            const idCell = row.querySelector('td:first-child');
            if (idCell && parseFloat(idCell.innerText) < 0) {
                row.classList.add('no-video');
            } else {
                row.classList.remove('no-video');
            }
        });
    }, 150);
}
"""

SEEK_JS = """
(filepath, seek_secs, vtt_content, playback_result) => {
    if (playback_result === 'external_ok' || playback_result === 'external_err' || playback_result === 'no_video') {
        return [filepath, seek_secs, vtt_content, playback_result];
    }
    setTimeout(() => {
        const container = document.querySelector('#anime-player');
        if (!container) return [filepath, seek_secs, vtt_content];
        const v = container.querySelector('video');
        if (!v) return [filepath, seek_secs, vtt_content];

        // Inject subtitle track
        container.querySelectorAll('track').forEach(t => t.remove());
        if (vtt_content && vtt_content.trim()) {
            const blob = new Blob([vtt_content], { type: 'text/vtt' });
            const url  = URL.createObjectURL(blob);
            const track = document.createElement('track');
            track.kind    = 'subtitles';
            track.label   = 'Japanese';
            track.srclang = 'ja';
            track.src     = url;
            track.default = false;   // off by default; user toggles
            v.appendChild(track);
            // Keep blob URL alive for the session (small, fine to not revoke)
        }

        // Seek and play
        const applySeek = () => { v.currentTime = seek_secs; v.play(); };
        if (v.readyState >= 1) {
            applySeek();
        } else {
            v.addEventListener('loadedmetadata', applySeek, { once: true });
        }

        // Add/update subtitle toggle button below the player
        const playerId = 'animangadex-sub-toggle';
        let btn = document.getElementById(playerId);
        if (!btn) {
            btn = document.createElement('button');
            btn.id = playerId;
            btn.style.cssText = 'margin-top:8px;padding:6px 14px;border-radius:6px;border:1px solid #888;background:#333;color:#fff;cursor:pointer;font-size:0.85rem;font-weight:600;';
            container.parentNode.insertBefore(btn, container.nextSibling);
        }
        const updateBtn = () => {
            const track = v.textTracks[0];
            const on = track && track.mode === 'showing';
            btn.textContent = on ? 'Subtitles: ON' : 'Subtitles: OFF';
            btn.style.background = on ? '#c0392b' : '#333';
        };
        btn.onclick = () => {
            const track = v.textTracks[0];
            if (!track) return;
            track.mode = track.mode === 'showing' ? 'hidden' : 'showing';
            updateBtn();
        };
        // Initial state
        if (v.textTracks.length > 0) {
            updateBtn();
        } else {
            v.addEventListener('loadedmetadata', updateBtn, { once: true });
        }

    }, 300);
    return [filepath, seek_secs, vtt_content, playback_result];
}
"""

def build_ui():
    with gr.Blocks(title="Animangadex") as demo:

        gr.HTML("""
            <div style="text-align:center;padding:1rem 0 0.5rem">
                <h1 style="font-size:2rem;font-weight:700">Animangadex</h1>
                <p style="color:#888;font-size:0.95rem">Search Japanese text across manga panels and anime subtitles.</p>
            </div>
        """)

        with gr.Tabs():

            # Tab 1: Index
            with gr.Tab("Index"):
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### Manga Panels")
                        gr.Markdown("Point to a folder of panel images. OCR runs locally via **manga-ocr** — first run downloads ~400 MB.")
                        manga_folder = gr.Textbox(label="Panel Folder Path", placeholder="/path/to/gochiusa/panels")
                        manga_index_btn = gr.Button("Index Panels", variant="primary")
                        manga_index_status = gr.Textbox(label="Status", interactive=False, lines=5)
                        manga_index_btn.click(fn=index_folder, inputs=manga_folder, outputs=manga_index_status)

                    with gr.Column():
                        gr.Markdown("### Anime Subtitles")
                        gr.Markdown(
                            "Point to a folder containing `.srt` or `.ass` subtitle files. "
                            "Video files should be in the **same folder** with the **same filename** stem."
                        )
                        anime_folder = gr.Textbox(label="Subtitle/Video Folder Path", placeholder="/path/to/gochiusa/episodes")
                        anime_index_btn = gr.Button("Index Subtitles", variant="primary")
                        anime_index_status = gr.Textbox(label="Status", interactive=False, lines=5)
                        anime_index_btn.click(fn=index_subtitles, inputs=anime_folder, outputs=anime_index_status)

            # Tab 2: Manga Search
            with gr.Tab("Manga Search"):
                with gr.Row():
                    manga_search_input = gr.Textbox(label="Search (Japanese)", placeholder="...", scale=4)
                    manga_max = gr.Slider(label="Max results", minimum=5, maximum=100, step=5, value=20, scale=1)
                    manga_search_btn = gr.Button("Search", variant="primary", scale=1)
                manga_status = gr.Textbox(label="Results", interactive=False, lines=1, elem_classes=["status-text"])
                gallery = gr.Gallery(label="Matching Panels", columns=3, height=620, object_fit="contain")
                manga_search_btn.click(fn=search_panels, inputs=[manga_search_input, manga_max], outputs=[gallery, manga_status])
                manga_search_input.submit(fn=search_panels, inputs=[manga_search_input, manga_max], outputs=[gallery, manga_status])

            # Tab 3: Anime Search
            with gr.Tab("Anime Search"):
                with gr.Row():
                    anime_search_input = gr.Textbox(label="Search (Japanese)", placeholder="...", scale=4)
                    anime_max = gr.Slider(label="Max results", minimum=5, maximum=100, step=5, value=30, scale=1)
                    anime_search_btn = gr.Button("Search", variant="primary", scale=1)

                anime_status = gr.Textbox(label="Results", interactive=False, lines=1, elem_classes=["status-text"])

                with gr.Row():
                    with gr.Column(scale=3):
                        results_table = gr.Dataframe(
                            elem_id="anime-results",
                            headers=["id", "Episode", "Timestamp", "Line"],
                            datatype=["number", "str", "str", "str"],
                            column_count=(4, "fixed"),
                            interactive=False,
                            wrap=True,
                        )
                        gr.Markdown("*Click any row to load the scene in the player.*")

                    with gr.Column(scale=2):
                        player_label = gr.Textbox(label="Now playing", interactive=False, lines=1)
                        video_player = gr.Video(
                            label="Scene",
                            interactive=False,
                            autoplay=True,
                            elem_id="anime-player",
                        )
                        # Hidden components
                        seek_time = gr.Number(value=0.0, visible=False)
                        vtt_content = gr.Textbox(value="", visible=False)
                        playback_mode_state = gr.Textbox(value="", visible=False)
                        playback_result = gr.Textbox(value="", visible=False)

                anime_search_btn.click(
                    fn=search_subtitles, inputs=[anime_search_input, anime_max], outputs=[results_table, anime_status]
                ).then(fn=None, inputs=[], outputs=[], js=TABLE_JS)
                anime_search_input.submit(
                    fn=search_subtitles, inputs=[anime_search_input, anime_max], outputs=[results_table, anime_status]
                ).then(fn=None, inputs=[], outputs=[], js=TABLE_JS)

                results_table.select(
                    fn=load_scene,
                    inputs=[results_table, playback_mode_state],
                    outputs=[video_player, seek_time, player_label, vtt_content, playback_result],
                ).then(
                    fn=None,
                    inputs=[video_player, seek_time, vtt_content, playback_result],
                    outputs=[video_player, seek_time, vtt_content, playback_result],
                    js=SEEK_JS,
                )

            # Tab 4: Settings
            with gr.Tab("Settings"):
                gr.Markdown("### Playback")
                _s = load_settings()
                playback_radio = gr.Radio(
                    choices=[("In-browser", "browser"), ("mpv", "mpv"), ("VLC", "vlc")],
                    value=_s["playback_mode"],
                    label="Playback mode",
                )
                player_paths_group = gr.Group(visible=_s["playback_mode"] != "browser")
                with player_paths_group:
                    mpv_path_input = gr.Textbox(label="mpv executable path", value=_s["mpv_path"])
                    vlc_path_input = gr.Textbox(label="VLC executable path", value=_s["vlc_path"])

                save_btn = gr.Button("Save Settings", variant="primary")
                settings_status = gr.Textbox(label="", interactive=False, lines=1, elem_classes=["status-text"])

                # Show/hide path fields based on mode selection
                playback_radio.change(
                    fn=lambda m: gr.update(visible=m != "browser"),
                    inputs=[playback_radio],
                    outputs=[player_paths_group],
                )
                # Also sync the hidden state used by load_scene
                playback_radio.change(
                    fn=lambda m: m,
                    inputs=[playback_radio],
                    outputs=[playback_mode_state],
                )
                save_btn.click(
                    fn=save_settings,
                    inputs=[playback_radio, mpv_path_input, vlc_path_input],
                    outputs=[settings_status],
                )

            # Tab 5: Stats / Manage
            with gr.Tab("Stats & Manage"):
                refresh_btn = gr.Button("Refresh Stats")
                stats_out = gr.Markdown()
                refresh_btn.click(fn=get_stats, outputs=stats_out)
                gr.Markdown("---")
                gr.HTML("<p style='color:#e53e3e;font-weight:700;font-size:1rem;margin:4px 0'>⚠ Danger Zone</p>")
                with gr.Row():
                    clear_manga_btn = gr.Button("Clear Manga Index", variant="stop")
                    clear_anime_btn = gr.Button("Clear Anime Index", variant="stop")
                clear_status = gr.Textbox(label="", interactive=False, lines=1, elem_classes=["status-text"])
                clear_manga_btn.click(fn=clear_manga_index, outputs=clear_status)
                clear_anime_btn.click(fn=clear_anime_index, outputs=clear_status)

        demo.load(fn=get_stats, outputs=stats_out)

    return demo


if __name__ == "__main__":
    init_db()
    ui = build_ui()
    ui.launch(
        allowed_paths=get_allowed_paths(),
        inbrowser=True,
        theme=gr.themes.Base(
            primary_hue="rose",
            neutral_hue="zinc",
            font=gr.themes.GoogleFont("Noto Sans JP"),
        ),
        css="""
            .gradio-container { max-width: 1200px; margin: auto; }
            #anime-player video { width: 100%; border-radius: 8px; }
            .tab-container { display: flex !important; justify-content: center !important; }

            /* Status textboxes - dimmer, smaller, no border */
            .status-text textarea, .status-text input {
                font-size: 0.82rem !important;
                color: #888 !important;
                border: none !important;
                background: transparent !important;
                box-shadow: none !important;
                padding: 2px 0 !important;
                resize: none !important;
            }
            .status-text { border: none !important; box-shadow: none !important; }
            /* Danger zone buttons */
            button.stop { background: #e53e3e !important; border-color: #c53030 !important; color: #fff !important; }
            button.stop:hover { background: #c53030 !important; }
            .status-text label span { display: none; }
            /* Gray out rows where video is missing (id encoded as negative) */
            #anime-results tr.no-video td { color: #999 !important; font-style: italic; }
        """,
    )
