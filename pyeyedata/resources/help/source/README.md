# Offline guide source and rebuild

PYeyedata-user-guide-zh-base.pdf is the retained eight-page C03 guide input.
Its SHA-256 is
d40f3b57d15a451a8f4333a9e549c11fa69b748ce4a62788f0ce9716390a686f.
The original pages remain vector/text PDF content; the 0.9.0 builder overlays
versioned footers and the About/license note on page 8 with pypdf.

From the repository root, run pyeyedata/resources/help/build_user_guide.py
with a PDF QA Python environment containing exactly pypdf 6.10.0 and
ReportLab 4.4.9. The checked-in output is
pyeyedata/resources/help/PYeyedata-user-guide-zh.pdf; the generated hash and
tool versions are recorded in guide-build-manifest.json.

The QA environment is only for authoring and rendering this release resource.
It is separate from the application's locked virtual environment and neither
PDF package is a product dependency or a frozen application component.
