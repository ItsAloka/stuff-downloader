# Spotify live sample, 2026-09-23

Ran `.venv/Scripts/python.exe .devteam-smoke/run.py` against the installed spotDL
worker. Source Spotify URLs and complete worker events are in each language JSON.
The downloaded MP3s remain in the language folders for review.

| Language | Spotify / YouTube result | Spotify / file seconds | Assessment |
| --- | --- | --- | --- |
| Japanese | YOASOBI, Idol / YOASOBI, Idol | 213.2 / 213.3 | Artist, title, and duration align; download succeeded after a Windows file-lock retry. |
| Chinese | Jay Chou, 告白氣球 / EnjoyLife lyric video | 215.1 / 215.7 | **Potential wrong recording**. Third-party lyric upload scored 94.2 and was not flagged. Metadata and duration alone do not prove audio identity. |
| English | Leah Kate, 10 Things I Hate About You / Leah Kate, same title | 157.1 / 157.2 | Artist, title, and duration align; download succeeded. |
| Sinhala | Yohani, Manike Mage Hithe / Chamath Sangeeth, Yohani, Satheeshan, same title | 167.6 / 162.8 | Title and Yohani align, but recording length is 4.8 s shorter. Download succeeded; audio identity is not independently verified. |

The worker retagged all four MP3s with Spotify metadata. This verifies tagging, but
Spotify's protected audio could not be compared directly to the downloaded audio.
The Chinese false certainty warrants a matching-rule change and another live check.
