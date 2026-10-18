# Offline guide source and rebuild

The 0.9.2 guide is generated from the same three maintained Markdown files shown
by the application's F1 help: quick-start-zh.md, input-files-zh.md and
plans-and-archives-zh.md. Examples are rendered as readable field/value tables.

From the repository root, run `python -X utf8 -B
pyeyedata/resources/help/build_user_guide.py` using the existing pinned ReportLab
4.2.5 environment. No new runtime dependency is required. Output and input/font
hashes, page count and tool version are recorded in guide-build-manifest.json.
Render the resulting PDF and inspect every page before release.

PYeyedata-user-guide-zh-base.pdf is the retained historical C03 guide source. It
is not used to generate the 0.9.2 guide; earlier editions can still be compared
against that original eight-page source.
