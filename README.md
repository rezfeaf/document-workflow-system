# DocPortal — Document Workflow & Archiving System

A full-stack internal document management system built with Flask and SQL Server. Originally built as a real-world production system during an internship; this is a sanitized/portfolio version with all company-specific branding, credentials, and internal network details removed.

## Features

- Document archiving with OCR search (Tesseract + EasyOCR, English/Arabic)
- Multi-step approval workflow engine (send, approve, reject, forward, resubmit) with full history and status tracking
- Real-time in-app notifications and messaging via Socket.IO
- Per-user email notifications (SMTP or Microsoft Graph), with encrypted credential storage
- USB/network scanner integration via a companion desktop agent
- QR code generation for shareable document links
- Audit logging, folder browser, and custom department-level fields
- Bilingual UI (English/Arabic) with RTL support and light/dark themes

## Tech stack

- Backend: Flask, pyodbc (SQL Server), Flask-SocketIO, waitress
- Auth/crypto: cryptography (Fernet) for at-rest credential encryption
- OCR: Tesseract, EasyOCR, pdf2image, pypdf
- Frontend: vanilla JS/HTML/CSS

## Setup

1. `cp .env.example .env` and fill in your own values.
2. `pip install -r requirements.txt --break-system-packages` (or use a virtualenv).
3. Install system dependencies separately (see comments in `requirements.txt`): `tesseract-ocr`, `tesseract-ocr-ara`, `poppler-utils`.
4. Point `SQLSERVER_*` env vars at your own SQL Server instance and schema.
5. Run with `python server.py`.

## Notes

This repo has had all company names, internal IP addresses, and default credentials removed or replaced with placeholders. It's meant to demonstrate architecture and implementation, not to be a drop-in deployable product for a specific organization.
