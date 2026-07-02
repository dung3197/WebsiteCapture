# website_capture

Capture rendered AWS Skill Builder-style course pages into clean Markdown plus local assets.

## Setup

```bash
cd /home/dyan/website_capture
python -m venv .venv
.venv/bin/pip install -e .
```

If you use fish shell, do not run `. .venv/bin/activate`. Use either:

```fish
source .venv/bin/activate.fish
```

or call the CLI directly with `.venv/bin/capture-url`.

## Capture One Page

```bash
cd /home/dyan/website_capture
export DISPLAY=:1 XAUTHORITY=/run/user/1000/xauth_JZmopq XDG_RUNTIME_DIR=/run/user/1000

.venv/bin/capture-url '<URL>' \
  --out output/page-capture \
  --delete-orphans \
  --headed \
  --wait-login-seconds 300 \
  --settle-seconds 45 \
  --min-text-chars 2000 \
  --markdown-only
```

## Capture Course Navigation

Use this when the page has a Course Navigation menu and each lesson should become its own Markdown file with its own assets directory.

```bash
cd /home/dyan/website_capture
export DISPLAY=:1 XAUTHORITY=/run/user/1000/xauth_JZmopq XDG_RUNTIME_DIR=/run/user/1000

.venv/bin/capture-url '<URL>' \
  --out output/aws-course-split \
  --delete-orphans \
  --headed \
  --wait-login-seconds 300 \
  --settle-seconds 45 \
  --min-text-chars 2000 \
  --split-course-navigation
```

Expected output shape:

```text
output/aws-course-split/
  Building a Data Warehouse Solution/
    Course Navigation.md
    Introduction/
      Introduction.md
      assets/
    Ingesting Data/
      Ingesting Data.md
      assets/
```

Split mode writes Markdown and assets only. It skips PDF, screenshot, and HTML output.

## Notes

- `--delete-orphans` deletes the selected output directory before capture, so stale lesson folders and old assets do not stay around after reruns.
- The first course/root navigation title becomes the parent folder.
- Lesson/action noise such as `GO TO WEBSITE`, video controls, zoom buttons, quiz buttons, markers, and flashcard controls is not split into separate lesson folders.
- Markdown cleanup keeps AWS course content readable: nested tab labels become smaller headings, batch-card images are resized and placed below titles, knowledge checks use checkboxes, and inline bold labels are split onto their own line.

