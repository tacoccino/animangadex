# Animangadex

Search Japanese text across manga panels and anime subtitles. Jump directly to the panel or scene where a word or phrase appears.

- **Manga:** panels are indexed locally using **manga-ocr**, a model fine-tuned for manga fonts, vertical text, and speech bubbles
- **Anime:** subtitle files (`.srt` / `.ass`) are parsed and indexed; clicking a result opens your video player at that exact timestamp

## Setup

```bash
# 1. (Recommended) Create a virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python app.py
```

The app opens in your browser at `http://localhost:7860`.

> **First run note:** manga-ocr downloads its model (~400 MB) on first use. This only happens once and is cached locally.

---

## Manga

### Indexing panels

- Go to the **Index** tab, **Manga Panels** column
- Enter the path to a folder containing your panel images
- Click **▶ Index Panels**
- Supported formats: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`
- Subfolders are scanned recursively — point at a whole series folder if you like
- Panels with no detected text are skipped automatically, so no need to curate your folder

Re-indexing the same folder is safe — existing entries are updated, not duplicated.

### Searching

- Go to the **Manga Search** tab
- Type a Japanese word or phrase (e.g. `コーヒー`, `ありがとう`, `チノ`)
- Matching panels appear as a gallery — click any to enlarge

### Folder structure tip

Organising panels by chapter or volume makes the filename shown in captions more useful:

```
gochiusa/
  vol01/
    ch001/
      001.png
      002.png
    ch002/
      001.png
  vol02/
    ...
```

Point the indexer at `gochiusa/` and it recurses through everything.

---

## Anime

### Preparing your files

The one rule: **subtitle file and video file must be in the same folder with the same filename stem.**

```
gochiusa/
  S01E01.mkv
  S01E01.ja.srt
  S01E02.mkv
  S01E02.ja.srt
```

Language suffixes on the subtitle file (`.ja`, `.jpn`, `.jp`) are stripped automatically when matching to a video, so `S01E01.ja.srt` will correctly pair with `S01E01.mkv`.

Nesting by season is fine — the indexer recurses through subfolders.

**Subtitle formats:** `.srt` and `.ass`/`.ssa` are both supported. `.ass` is more common for anime and is handled correctly — styling tags like `{\an8}` are stripped before indexing.

**Extracting subs from MKV:** if your subtitles are embedded in the `.mkv` rather than as a separate file, use ffmpeg to extract them:

```bash
ffmpeg -i episode.mkv -map 0:s:0 episode.ja.srt
```

Change `s:0` to whichever subtitle track index is Japanese.

### Indexing subtitles

- Go to the **Index** tab, **Anime Subtitles** column
- Enter the path to your episodes folder
- Click **▶ Index Subtitles**

### Searching

- Go to the **Anime Search** tab
- Type a Japanese word or phrase
- Results show episode name, timestamp, and the matching line
- Click any row to open that scene in your video player

The player seeks 1 second before the line starts so you get a moment of context.

### Video player

The app tries **mpv** first, then falls back to **VLC**. mpv is recommended for its precise seeking. It checks common Windows install locations automatically (Program Files, Scoop), so it should be found even if not on your PATH. If neither is found, the error message will tell you what to do.

---

## Stats & Manage

The **Stats & Manage** tab shows how many panels and subtitle lines are indexed. The manga and anime indexes can be cleared independently.

---

## .gitignore note

The database file `manga_index.db` is excluded from version control — it's machine-specific and rebuilt by re-indexing.
