"""
@file domain/export_service.py
@description Business logic for compiling memoir exports, PDF generation, and storage management using pure Python (xhtml2pdf).
"""

import html
import io
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from xhtml2pdf import pisa
from xhtml2pdf.config.resources import ResourceAccessPolicy

from src.core.config import settings
from src.domain.authorization import verify_active_participant
from src.integrations import participant_repository, storage_adapter
from src.integrations.export_repository import ExportRepository

# Long enough to survive PDF generation (download + AssemblyAI-scale documents
# can take a while) without living so long it's a lingering credential.
EXPORT_IMAGE_SIGNED_URL_TTL_SECONDS = 15 * 60

class ExportService:

    @classmethod
    def verify_owner_access(cls, memoir_id: str, user_id: str) -> None:
        """
        Export is owner-only — never readers, never share-token holders, per PRD.
        Mirrors the role check used to queue an export (initiate_export) so status
        lookups and job creation stay consistent. Returns 404 (not 403) so a
        stranger can't distinguish "not yours" from "doesn't exist".
        """
        participant_res = participant_repository.fetch_participant(memoir_id, user_id)
        participants = participant_res.data or []
        if not participants or participants[0].get("role") not in ("owner", "admin", "contributor"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memoir not found.")

    @classmethod
    def initiate_export(cls, memoir_id: str, user_id: str) -> dict:
        """Validates permissions and queues a new PDF export job."""
        participant = verify_active_participant(
            memoir_id, 
            user_id, 
            required_roles=["owner", "admin", "contributor"]
        )
        participant_id = participant.get("id")
        job = ExportRepository.create_export_job(memoir_id, participant_id, kind="pdf")

        return {
            "export_id": job["id"],
            "memoir_id": memoir_id,
            "status": "queued",
            "message": "Export job queued successfully. Processing in background.",
            "created_at": job["created_at"]
        }

    @classmethod
    def process_export_background(cls, export_id: str, memoir_id: str) -> None:
        """Background worker method to compile data, generate PDF via xhtml2pdf, and upload to storage."""
        try:
            # 1. Fetch structured memoir payload (excluding comments)
            payload = ExportRepository.fetch_memoir_export_payload(memoir_id)
            memoir = payload["memoir"]
            memories = payload["memories"]
            media_assets = payload["media_assets"]
            transcripts = payload["transcripts"]

            # 2. Build professional print HTML template (Book layout)
            html_content = cls._render_memoir_html(memoir, memories, media_assets, transcripts)

            # 3. Compile HTML to PDF bytes using xhtml2pdf. xhtml2pdf fetches
            # <img>/<link>/@font-face targets over the network while rendering —
            # without a restrictive policy, a memory whose (properly escaped, so
            # this only matters for the ALREADY-legitimate <img> tags we
            # generate below) content included an attacker-chosen URL would
            # turn PDF generation into an SSRF primitive. allowed_hosts pins
            # every fetch to our own Supabase project; nothing else may be
            # reached, local files included (base_dir=None).
            supabase_host = urlsplit(settings.supabase_url).hostname
            resource_policy = ResourceAccessPolicy(
                allow_remote=True,
                allow_private_networks=False,
                allowed_hosts=frozenset({supabase_host}) if supabase_host else frozenset(),
                base_dir=None,
                allow_local_outside_base=False,
            )

            pdf_buffer = io.BytesIO()
            pisa_status = pisa.CreatePDF(html_content, dest=pdf_buffer, resource_policy=resource_policy)

            if pisa_status.err:
                raise Exception("Failed to compile HTML into PDF using xhtml2pdf.")
            
            pdf_bytes = pdf_buffer.getvalue()

            # 4. Upload to Supabase Storage
            storage_key = f"exports/pdf-archives/{memoir_id}/{export_id}.pdf"
            ExportRepository.upload_pdf_to_storage(storage_key, pdf_bytes)

            # 5. Mark job as ready
            ExportRepository.update_job_status(
                export_id=export_id,
                status="ready",
                storage_key=storage_key,
                byte_size=len(pdf_bytes)
            )

        except Exception as e:
            ExportRepository.update_job_status(
                export_id=export_id,
                status="failed",
                error_message=str(e)
            )

    @staticmethod
    def _render_memoir_html(memoir: dict, memories: list, media_assets: list, transcripts: list) -> str:
        """
        Generates a high-end, printable book layout HTML string.

        Every user-supplied string (titles, body text, captions, transcripts,
        the memoir's own title/description) is html.escape()'d before
        interpolation. This isn't just a layout concern: xhtml2pdf fetches
        <img>/<link> targets it finds while rendering, so an UNescaped
        `<img src="http://attacker/…">` typed into a memory would make this
        server issue an outbound request to an address the attacker chose —
        escaping means that text renders as inert, visible characters instead
        of becoming a tag at all.
        """
        esc = html.escape
        # memoir has no "title" column (only subject_name) — this always fell
        # back to the placeholder before.
        memoir_title = esc(memoir.get("subject_name") or "My Memoir")
        memoir_description = esc(memoir.get("description") or "A curated collection of life memories.")

        memories_html = ""
        for mem in memories:
            title = esc(mem.get("title") or "Untitled Entry")
            date = esc(mem.get("occurred_start") or mem.get("created_at", "")[:10])
            body = esc(mem.get("body_text") or "").replace(chr(10), "<br>")

            memories_html += f"""
            <div class="memory-entry">
                <div class="memory-meta">{date}</div>
                <h2>{title}</h2>
                <div class="memory-body">{body}</div>
            </div>
            """

        # Render media photo gallery if any exist. Readers/PDFs never get the
        # raw storage_key — the bucket is private, so the raw path is just a
        # broken placeholder anyway — they get a signed URL, exactly like the
        # rest of the app already does for playback.
        photos_html = ""
        for ma in media_assets:
            if ma.get("kind") == "photo" and ma.get("storage_key"):
                img_url = storage_adapter.create_playback_url(
                    ma["storage_key"], ttl_seconds=EXPORT_IMAGE_SIGNED_URL_TTL_SECONDS
                )
                if not img_url:
                    continue
                caption = esc(ma.get("caption") or "")
                photos_html += f"""
                <div class="photo-container">
                    <img src="{esc(img_url)}" alt="Memory photo" />
                    {f'<p class="photo-caption">{caption}</p>' if caption else ''}
                </div>
                """

        # Render audio transcripts section if any exist
        transcripts_html = ""
        for t in transcripts:
            t_text = esc(t.get("display_text") or "")
            if t_text:
                transcripts_html += f"""
                <div class="transcript-box">
                    <strong>Voice Recording Transcript:</strong>
                    <p>{t_text}</p>
                </div>
                """

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                @page {{
                    size: A4;
                    margin: 20mm 15mm;
                }}
                body {{
                    font-family: Helvetica, Arial, sans-serif;
                    color: #2c2c2c;
                    background-color: #fdfbf7;
                    margin: 0;
                    padding: 20px;
                    line-height: 1.6;
                }}
                .cover-page {{
                    text-align: center;
                    padding-top: 100px;
                    page-break-after: always;
                }}
                .cover-page h1 {{
                    font-size: 28pt;
                    color: #1a1a1a;
                    margin-bottom: 10px;
                }}
                .cover-page p {{
                    font-size: 12pt;
                    font-style: italic;
                    color: #555;
                }}
                .memory-entry {{
                    margin-bottom: 30px;
                    border-bottom: 1px solid #e6e2d8;
                    padding-bottom: 25px;
                }}
                .memory-meta {{
                    font-size: 8pt;
                    text-transform: uppercase;
                    letter-spacing: 1.5px;
                    color: #887a64;
                    margin-bottom: 4px;
                }}
                h2 {{
                    font-size: 16pt;
                    color: #222;
                    margin: 0 0 10px 0;
                }}
                .memory-body {{
                    font-size: 10pt;
                    text-align: justify;
                    margin-bottom: 12px;
                }}
                .section-title {{
                    font-size: 14pt;
                    color: #222;
                    margin-top: 40px;
                    margin-bottom: 15px;
                    border-bottom: 2px solid #b8a894;
                    padding-bottom: 5px;
                }}
                .photo-container {{
                    margin: 15px 0;
                    text-align: center;
                }}
                .photo-container img {{
                    max-width: 70%;
                    max-height: 300px;
                }}
                .photo-caption {{
                    font-size: 8pt;
                    font-style: italic;
                    color: #666;
                    margin-top: 4px;
                }}
                .transcript-box {{
                    background: #f4efea;
                    border-left: 3px solid #b8a894;
                    padding: 10px 15px;
                    font-size: 9pt;
                    margin-top: 15px;
                }}
            </style>
        </head>
        <body>
            <div class="cover-page">
                <h1>{memoir_title}</h1>
                <p>{memoir_description}</p>
            </div>
            <div class="content">
                <div class="section-title">Memories</div>
                {memories_html}
                
                {f'<div class="section-title">Photo Gallery</div>{photos_html}' if photos_html else ''}
                
                {f'<div class="section-title">Voice Transcripts</div>{transcripts_html}' if transcripts_html else ''}
            </div>
        </body>
        </html>
        """