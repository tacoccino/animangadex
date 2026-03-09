# Manga OCR Search

Search manga panels by Japanese text. OCR is handled locally by **manga-ocr**, a model fine-tuned specifically for manga fonts, vertical text, and speech bubbles.

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

## Usage

### 1. Index your panels

- Go to the **Index Panels** tab
- Enter the path to a folder containing your panel images
  - e.g. `/home/you/gochiusa/ch01/` or `C:\Manga\Gochiusa\Vol1\panels`
- Click **▶ Index**
- Supported formats: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`
- Subfolders are scanned recursively, so you can point at a whole series folder

Re-indexing the same folder is safe — existing entries are updated, not duplicated.

### 2. Search

- Go to the **Search** tab
- Type a Japanese word or phrase (e.g. `コーヒー`, `ありがとう`, `チノ`)
- Matching panels appear as a gallery — click any to enlarge

### 3. Stats & Manage

- See how many panels are indexed
- Clear the entire index if you want to start fresh

---

## Folder structure tips

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

## Extending to anime (coming next)

The anime side will parse `.srt` / `.ass` subtitle files, index lines into the same SQLite database, and let you click a result to open your video player at the right timestamp.
