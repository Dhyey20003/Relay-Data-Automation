# Third-party software notices

Relay Employee Hub uses only free, open-source components. No paid APIs, cloud
services, telemetry, CDNs or external AI services are used. The browser
interface (HTML, CSS, JavaScript) is written for this application and served
locally; it contains no third-party frontend code.

## Runtime dependencies (bundled in the packaged build)

| Component | Version | License | Purpose |
|---|---|---|---|
| FastAPI | 0.141.1 | MIT | Web framework |
| Starlette | 1.7.0 | BSD-3-Clause | ASGI toolkit used by FastAPI |
| Pydantic / pydantic-core | 2.13.5 / 2.46.5 | MIT | Request validation |
| annotated-types, annotated-doc, typing-inspection | 0.8.0, 0.0.5, 0.4.4 | MIT | Typing helpers |
| typing_extensions | 4.16.0 | PSF-2.0 | Typing backports |
| AnyIO | 4.15.1 | MIT | Async support |
| idna | 3.20 | BSD-3-Clause | Host name handling |
| Uvicorn | 0.54.0 | BSD-3-Clause | Local web server |
| h11 | 0.16.0 | MIT | HTTP/1.1 protocol |
| Click | 8.5.0 | BSD-3-Clause | Used by Uvicorn |
| colorama (Windows only) | 0.4.6 | BSD-3-Clause | Used by Click |
| python-multipart | 0.0.32 | Apache-2.0 | File uploads |
| openpyxl | 3.1.5 | MIT | Reading/writing .xlsx (never runs formulas or macros) |
| et_xmlfile | 2.0.0 | MIT | Used by openpyxl |
| Pillow | 12.3.0 | MIT-CMU (HPND) | Image validation, thumbnails, JPG conversion |
| pypdfium2 | 5.13.0 | Apache-2.0 / BSD-3-Clause | PDF rendering and extraction |
| PDFium (inside pypdfium2) | bundled | BSD-3-Clause / Apache-2.0 and bundled third-party licenses | PDF engine by Google |
| tzdata | 2026.4 | Apache-2.0 | Time-zone database (Asia/Kolkata on Windows) |
| pywin32 (Windows only) | 312 | PSF-2.0 | Classic Outlook COM automation |
| Python runtime | 3.12 (packaged build) | PSF-2.0 | Interpreter embedded in the executable |

## Build and test tools (not shipped)

| Component | Version | License |
|---|---|---|
| PyInstaller | 6.22.3 | GPL-2.0 with the PyInstaller bootloader exception (the packaged application may be distributed under any license) |
| pyinstaller-hooks-contrib, altgraph | 2026.7, 0.17.5 | Apache-2.0/GPL-2.0, MIT |
| pytest (+ iniconfig, pluggy, packaging, Pygments) | 9.1.1 | MIT and compatible |
| httpx / httpcore / certifi | 0.28.1 / 1.0.9 / 2026.7.22 | BSD-3-Clause / BSD-3-Clause / MPL-2.0 |

The full license texts are included with each package's distribution
(for example in the `*.dist-info` folders of the Python environment and in the
`_internal` folder of the packaged build). pypdfium2 ships PDFium's license and
the licenses of PDFium's own dependencies in its distribution.
