import os
import io
import re
import json
import time
import uuid
import base64
import shutil
import requests
from flask import Flask, render_template, request, jsonify
from PIL import Image

try:
    import fitz  # PyMuPDF untuk membaca file PDF multi-halaman
except ImportError:
    fitz = None

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
CACHED_MODELS = []
JOB_BASE_DIR = "/tmp/ibs_jobs"
os.makedirs(JOB_BASE_DIR, exist_ok=True)

SYSTEM_INSTRUCTION = """
Kamu adalah Senior RF Engineer IBS (In-Building System) yang bertugas mengekstrak topologi jaringan dari skematik multi-lantai menjadi JSON terstruktur untuk setiap titik antena indoor dengan ketelitian tinggi (>95%).

ATURAN EKSTRAKSI (WAJIB JSON VALID):
1. HANYA keluarkan JSON murni (tanpa markdown ```json dan tanpa teks tambahan).
2. ATURAN PENELUSURAN JALUR (END-TO-END PER ANTENA):
   - HANYA ekstrak titik Antena Indoor distribusi (AO-1, AO-2, AP-1, dst.). DILARANG memasukkan Repeater, MCM/Combiner/HC, Splitter, Coupler, kabel riser (TO LT / FROM LT), maupun Antenna Donor sebagai baris antena.
   - Telusuri jalur MULAI DARI port output sumber utama (MCM/Combiner/HC atau Repeater) sampai ke masing-masing titik antena:
     * TITIK AWAL (WAJIB): Sertakan segmen kabel pertama tepat setelah port output sumber utama (MCM/Combiner/HC/Repeater) dan splitter percabangan utama pertama yang membagi jalur ke tiap lantai. Hitung dengan sangat teliti seluruh cabang output splitter utama tersebut (termasuk garis riser vertikal yang digambar berdekatan/sejajar turun ke lantai-lantai di bawahnya: total 2 output = "S2", 3 output = "S3", 4 output = "S4").
     * PENCOCOKAN RISER LINTAS GAMBAR: Pastikan setiap label asal riser pada gambar lantai tujuan (misal "FROM LT X") dicocokkan persis dengan label tujuan riser yang sesuai pada gambar sebelumnya (misal "TO LT Y") beserta ukuran dan panjang meternya—jangan pernah menukar kabel riser antar-lantai.
     * VERIFIKASI JUMLAH CABANG SPLITTER (KONSISTENSI TOPOLOGI): Untuk setiap simbol Power Splitter yang dilewati, hitung secara fisik berapa banyak garis output yang keluar dari simbol tersebut (2 cabang = "S2", 3 cabang = "S3", 4 cabang = "S4"). Semua titik antena yang bercabang dari satu simbol splitter akhir yang sama WAJIB memiliki kode splitter terakhir yang sama dan panjang kabel cabangnya dibaca satu per satu sesuai angka pada masing-masing garis antena.
   - "a": kode titik antena sesuai label gambar (gunakan awalan lantai seperti LT4-AO-1, LT3-AO-1, LT2-AO-1 jika nomor antena berulang antar-lantai).
   - "f": posisi lantai dengan huruf kapital (contoh: LANTAI 4, LANTAI 3, LANTAI 2).
   - "t": jenis antena dengan rapi (Antenna Omni atau Antenna Planar).
   - "c": list seluruh segmen kabel [ukuran, panjang_meter] mulai dari output sumber utama hingga antena. Ukuran hanya boleh: "1/2", "7/8", atau "1-1/4".
   - "d": list seluruh kode perangkat pasif G-Wave mulai dari splitter pertama setelah output sumber utama hingga antena (JANGAN masukkan MCM/Combiner/HC):
     * "S2" = Power Splitter 2-Way
     * "S3" = Power Splitter 3-Way
     * "S4" = Power Splitter 4-Way
     * "C5_C" / "C5_T" = Coupler 5 dB (Coupled / Through)
     * "C6_C" / "C6_T" = Coupler 6 dB (Coupled / Through)
     * "C7_C" / "C7_T" = Coupler 7 dB (Coupled / Through)
     * "C10_C" / "C10_T" = Coupler 10 dB (Coupled / Through)
     * "C15_C" / "C15_T" = Coupler 15 dB (Coupled / Through)
     * "C20_C" / "C20_T" = Coupler 20 dB (Coupled / Through)
     * "C30_C" / "C30_T" = Coupler 30 dB (Coupled / Through)
"""

CABLE_LOSS_PER_M = {
    "1/2": 0.07,
    "0.5": 0.07,
    "7/8": 0.04,
    "0.875": 0.04,
    "1-1/4": 0.03,
    "1 1/4": 0.03,
    "1.25": 0.03,
}

DEVICE_LOSS_DB = {
    "S2": 3.5, "SPLITTER_2WAY": 3.5,
    "S3": 5.5, "SPLITTER_3WAY": 5.5,
    "S4": 6.5, "SPLITTER_4WAY": 6.5,
    "C5_C": 5.5, "C5_T": 1.8,
    "C6_C": 6.5, "C6_T": 1.5,
    "C7_C": 7.5, "C7_T": 1.2,
    "C10_C": 10.5, "C10_T": 0.7,
    "C15_C": 15.4, "C15_T": 0.4,
    "C20_C": 20.3, "C20_T": 0.3,
    "C30_C": 30.3, "C30_T": 0.2,
}

def cleanup_old_jobs(max_age_sec=3600):
    now = time.time()
    try:
        for name in os.listdir(JOB_BASE_DIR):
            path = os.path.join(JOB_BASE_DIR, name)
            if os.path.isdir(path) and (now - os.path.getmtime(path) > max_age_sec):
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass

def get_job_dir(job_id):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", job_id)
    return os.path.join(JOB_BASE_DIR, safe_id)

def load_job_state(job_id):
    state_file = os.path.join(get_job_dir(job_id), "state.json")
    if not os.path.exists(state_file):
        return None
    with open(state_file, "r", encoding="utf-8") as f:
        return json.load(f)

def save_job_state(job_id, state):
    jdir = get_job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    state_file = os.path.join(jdir, "state.json")
    tmp_file = state_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp_file, state_file)

def get_available_gemini_models():
    global CACHED_MODELS
    if CACHED_MODELS:
        return CACHED_MODELS

    base_host = "generativelanguage.googleapis.com"
    list_url = f"https://{base_host}/v1beta/models?key={GEMINI_API_KEY}"
    blacklist = [
        "embedding", "aqa", "imagen", "veo", "tts", "audio",
        "robotics", "computer-use", "image", "2.0", "2.5", "lite"
    ]
    try:
        resp = requests.get(list_url, timeout=10)
        if resp.status_code == 200:
            models_data = resp.json().get("models", [])
            valid_names = []
            for m in models_data:
                methods = m.get("supportedGenerationMethods", [])
                name = m.get("name", "").replace("models/", "")
                if "generateContent" in methods and "gemini" in name:
                    if not any(bad in name for bad in blacklist):
                        valid_names.append(name)

            def model_priority(n):
                if n == "gemini-flash-latest":
                    return 0
                if "3." in n and "flash" in n:
                    return 1
                if "pro" in n:
                    return 2
                return 3

            valid_names.sort(key=model_priority)
            if valid_names:
                CACHED_MODELS = valid_names[:2]
                return CACHED_MODELS
    except Exception:
        pass

    return ["gemini-flash-latest", "gemini-pro-latest"]

def encode_image_jpeg_b64(img):
    img = img.convert("RGB")
    img.thumbnail((2250, 2250), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def pdf_bytes_to_b64_images(pdf_bytes):
    if fitz is None:
        raise RuntimeError(
            "Pustaka 'pymupdf' belum terpasang di server. "
            "Jalankan 'pip install pymupdf' di Bash console PythonAnywhere."
        )
    images_b64 = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            # Render 220 DPI agar angka kabel & garis cabang splitter pada PDF vektor tetap sangat tajam
            pix = page.get_pixmap(dpi=220, alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            images_b64.append(encode_image_jpeg_b64(img))
    finally:
        doc.close()
    return images_b64

def parse_uploaded_images(req):
    b64_images = []
    if req.files:
        uploaded_files = req.files.getlist('files')
        if not uploaded_files or all(f.filename == '' for f in uploaded_files):
            single = req.files.get('file')
            if single and single.filename != '':
                uploaded_files = [single]
        for f in uploaded_files:
            if not f or f.filename == '':
                continue
            fname = f.filename.lower()
            mimetype = (f.mimetype or '').lower()
            if fname.endswith('.pdf') or mimetype == 'application/pdf':
                b64_images.extend(pdf_bytes_to_b64_images(f.read()))
            else:
                img = Image.open(f.stream)
                b64_images.append(encode_image_jpeg_b64(img))
    elif req.is_json:
        data = req.get_json(silent=True) or {}
        b64_list = data.get('images', [])
        if not b64_list and data.get('image'):
            b64_list = [data.get('image')]
        for b64_str in b64_list:
            if not b64_str:
                continue
            is_pdf_header = "application/pdf" in b64_str[:50].lower()
            if "," in b64_str:
                b64_str = b64_str.split(",", 1)[1]
            raw_bytes = base64.b64decode(b64_str)
            if is_pdf_header or raw_bytes[:4] == b"%PDF":
                b64_images.extend(pdf_bytes_to_b64_images(raw_bytes))
            else:
                img = Image.open(io.BytesIO(raw_bytes))
                b64_images.append(encode_image_jpeg_b64(img))
    return b64_images

def parse_json_payload(raw_text):
    text = raw_text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    obj_start = text.find("{")
    arr_start = text.find("[")
    if obj_start != -1 and (arr_start == -1 or obj_start < arr_start):
        obj_end = text.rfind("}")
        return json.loads(text[obj_start:obj_end + 1])
    elif arr_start != -1:
        arr_end = text.rfind("]")
        return {"antennas": json.loads(text[arr_start:arr_end + 1]), "risers": []}
    raise ValueError("Respons AI tidak mengandung struktur JSON yang valid.")

def calculate_rf_csv_from_antennas(items):
    rows = ["TITIK_ANTENA,LANTAI_POSISI,TIPE_ANTENA,FEEDER_LOSS_DB,SPLITTER_LOSS_DB,TOTAL_LOSS_DB"]

    for item in items:
        titik = str(item.get("a") or item.get("titik_antena", "")).strip().replace(",", "")
        lantai = str(item.get("f") or item.get("lantai_posisi", "")).strip().upper().replace(",", "")
        tipe = str(item.get("t") or item.get("tipe_antena", "Antenna Omni")).strip().replace(",", "")

        theo_feeder = 0.0
        for c in (item.get("c") or item.get("cables") or []):
            if isinstance(c, list) and len(c) >= 2:
                sz = str(c[0]).replace('"', '').strip()
                length_m = float(c[1])
            elif isinstance(c, dict):
                sz = str(c.get("size", "1/2")).replace('"', '').strip()
                length_m = float(c.get("length_m", 0))
            else:
                continue
            rate = CABLE_LOSS_PER_M.get(sz, 0.07)
            theo_feeder += length_m * rate

        feeder_loss = round(theo_feeder * 1.12, 2)

        devices = item.get("d") or item.get("devices") or []
        splitter_loss = 0.0
        for dev in devices:
            key = str(dev).strip().upper()
            splitter_loss += DEVICE_LOSS_DB.get(key, 0.0)
        splitter_loss = round(splitter_loss, 2)

        connector_loss = (len(devices) * 0.20) + 0.50
        total_loss = round(feeder_loss + splitter_loss + connector_loss, 2)

        rows.append(f"{titik},{lantai},{tipe},{feeder_loss:.2f},{splitter_loss:.1f},{total_loss:.2f}")

    return "\n".join(rows)

def call_gemini_step_safe(parts, step_label):
    candidate_models = get_available_gemini_models()
    last_error = None
    step_start = time.time()

    for model_name in candidate_models:
        elapsed = time.time() - step_start
        # Jaga total waktu 1 panggilan polling tidak pernah melewati 75 detik agar bebas HTTP 502 PythonAnywhere
        if elapsed > 50:
            break

        base_host = "generativelanguage.googleapis.com"
        url = f"https://{base_host}/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"

        payload_with_thinking = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.05,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "HIGH"}
            }
        }
        payload_basic = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json"
            }
        }

        current_payload = payload_with_thinking if "3." in model_name else payload_basic
        req_timeout = min(42, max(20, int(72 - elapsed)))

        try:
            resp = requests.post(url, json=current_payload, timeout=req_timeout)
            if resp.status_code == 200:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                if raw_text:
                    return parse_json_payload(raw_text)
            elif resp.status_code == 400 and "thinking" in resp.text.lower():
                resp2 = requests.post(url, json=payload_basic, timeout=req_timeout)
                if resp2.status_code == 200:
                    raw_text = resp2.json()["candidates"][0]["content"]["parts"][0]["text"]
                    if raw_text:
                        return parse_json_payload(raw_text)
            else:
                last_error = f"HTTP {resp.status_code} ({model_name}): {resp.text[:220]}"
        except Exception as err:
            last_error = f"{model_name}: {str(err)}"

    raise Exception(f"Gagal pada {step_label}: {last_error}")

def run_extract_pass(b64_img, img_idx, total_images, riser_context, user_message):
    if img_idx == 0:
        prompt_text = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            f"Ini adalah HALAMAN/GAMBAR KE-1 dari total {total_images} skematik IBS.\n"
            "Keluarkan JSON object dengan struktur:\n"
            '{"antennas":[{"a":"<kode_antena>","f":"<LANTAI_X>","t":"<tipe_antena>","c":[["<ukuran>",<meter>]],"d":["<kode_perangkat>"]}],'
            '"risers":[{"target":"<label_tujuan_riser>","c":[["<ukuran>",<meter>]],"d":["<kode_perangkat>"]}]}\n'
            "- 'antennas': HANYA titik antena indoor pada gambar ke-1 ini, ditelusuri mulai dari kabel pertama setelah output sumber utama dan splitter percabangan pertama. Periksa jumlah garis cabang keluar pada setiap simbol splitter akhir secara teliti (2 cabang = S2, 3 cabang = S3, 4 cabang = S4) dan baca panjang kabel masing-masing antena secara individual.\n"
            "- 'risers': daftar semua kabel riser keluar menuju gambar lantai lain beserta akumulasi kabel awal + kabel riser dan splitter awal yang sudah dilewatinya.\n"
        )
    else:
        riser_json_str = json.dumps(riser_context)
        prompt_text = (
            f"{SYSTEM_INSTRUCTION}\n\n"
            f"Ini adalah HALAMAN/GAMBAR KE-{img_idx + 1} dari total {total_images} skematik IBS.\n"
            f"REFERENSI RISER DARI GAMBAR SEBELUMNYA: {riser_json_str}\n\n"
            "Keluarkan JSON object dengan struktur:\n"
            '{"antennas":[{"a":"<kode_antena>","f":"<LANTAI_X>","t":"<tipe_antena>","c":[["<ukuran>",<meter>]],"d":["<kode_perangkat>"]}],"risers":[]}\n'
            "- Cocokkan setiap label asal riser pada gambar ini dengan 'target' yang sesuai pada REFERENSI RISER di atas, lalu gabungkan list 'c' dan 'd' dari referensi riser tersebut dengan kabel & splitter pada lantai ini untuk setiap antena (verifikasi jumlah cabang fisik pada tiap simbol splitter akhir satu per satu).\n"
        )

    if user_message:
        prompt_text += f"Catatan tambahan user: {user_message}\n"

    parts = [
        {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}},
        {"text": prompt_text}
    ]
    return call_gemini_step_safe(parts, f"Ekstraksi Halaman {img_idx + 1}")

def run_audit_pass(b64_img, img_idx, draft_payload, riser_context):
    audit_prompt = (
        f"{SYSTEM_INSTRUCTION}\n\n"
        f"TUGAS AUDIT AKURASI TINGGI (HALAMAN/GAMBAR KE-{img_idx + 1}):\n"
        "Berikut adalah hasil ekstraksi awal (DRAFT JSON) untuk gambar ini:\n"
        f"{json.dumps(draft_payload)}\n\n"
        f"Referensi Riser lintas gambar (jika ada): {json.dumps(riser_context)}\n\n"
        "Lakukan audit ulang secara ketat terhadap gambar skematik ini dan perbaiki setiap kesalahan pada DRAFT JSON di atas:\n"
        "1. AUDIT JUMLAH CABANG SPLITTER AKHIR: Lihat setiap simbol Power Splitter pada gambar ini dan hitung secara fisik berapa garis antena yang keluar dari simbol tersebut (2 garis = S2, 3 garis = S3, 4 garis = S4). Pastikan semua antena yang terhubung ke satu simbol splitter yang sama memiliki kode splitter yang identik dan sesuai dengan jumlah garis fisiknya!\n"
        "2. AUDIT PANJANG KABEL INDIVIDUAL: Cocokkan kembali label panjang kabel (meter) di setiap garis menuju masing-masing titik antena satu per satu agar tidak ada satu pun angka yang tertukar atau salah baca.\n"
        "3. AUDIT RISER & SPLITTER AWAL: Pastikan kabel awal setelah output sumber utama dan splitter pembagi antar-lantai sudah masuk lengkap dan tidak tertukar antar-lantai.\n"
        "Keluarkan JSON object final yang sudah diaudit dengan struktur persis sama (`antennas` dan `risers`)."
    )
    parts = [
        {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}},
        {"text": audit_prompt}
    ]
    return call_gemini_step_safe(parts, f"Audit Halaman {img_idx + 1}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        if not GEMINI_API_KEY:
            msg = "GEMINI_API_KEY belum diset di file WSGI PythonAnywhere."
            return jsonify({'status': 'error', 'response': msg, 'message': msg}), 500

        cleanup_old_jobs()
        b64_images = parse_uploaded_images(request)
        if not b64_images:
            msg = 'Silakan unggah 1 atau lebih gambar / file PDF skematik IBS terlebih dahulu.'
            return jsonify({'status': 'error', 'response': msg, 'message': msg}), 400

        user_message = (
            request.form.get('message', '')
            if not request.is_json
            else (request.get_json(silent=True) or {}).get('message', '')
        )

        job_id = str(uuid.uuid4())
        jdir = get_job_dir(job_id)
        os.makedirs(jdir, exist_ok=True)

        for idx, b64_str in enumerate(b64_images):
            with open(os.path.join(jdir, f"img_{idx}.b64"), "w", encoding="utf-8") as f:
                f.write(b64_str)

        state = {
            "status": "processing",
            "total_images": len(b64_images),
            "current_index": 0,
            "sub_phase": "extract",
            "draft_payload": None,
            "user_message": user_message,
            "antennas": [],
            "risers": [],
            "step": f"Dokumen tersimpan ({len(b64_images)} halaman). Tahap 1: Ekstraksi topologi 1/{len(b64_images)}..."
        }
        save_job_state(job_id, state)

        return jsonify({
            'status': 'processing',
            'job_id': job_id,
            'step': state["step"]
        })

    except Exception as e:
        err_msg = f"Error: {str(e)}"
        return jsonify({'status': 'error', 'message': err_msg, 'response': err_msg}), 500

@app.route('/api/job/<job_id>', methods=['GET'])
def job_status(job_id):
    state = load_job_state(job_id)
    if not state:
        return jsonify({'status': 'error', 'message': 'Job tidak ditemukan atau sudah kedaluwarsa.'}), 404

    if state["status"] == "done":
        csv_data = state["csv"]
        return jsonify({
            'status': 'success',
            'image_count': state["total_images"],
            'csv': csv_data,
            'reply': csv_data,
            'response': csv_data
        })

    if state["status"] == "error":
        return jsonify({
            'status': 'error',
            'message': state.get("message", "Terjadi kesalahan"),
            'response': state.get("message", "Terjadi kesalahan")
        })

    idx = state["current_index"]
    total = state["total_images"]
    sub_phase = state.get("sub_phase", "extract")
    jdir = get_job_dir(job_id)
    img_path = os.path.join(jdir, f"img_{idx}.b64")

    try:
        with open(img_path, "r", encoding="utf-8") as f:
            b64_img = f.read()

        if sub_phase == "extract":
            draft = run_extract_pass(
                b64_img=b64_img,
                img_idx=idx,
                total_images=total,
                riser_context=state.get("risers", []),
                user_message=state.get("user_message", "")
            )
            state["draft_payload"] = draft
            state["sub_phase"] = "audit"
            state["step"] = f"Ekstraksi halaman {idx + 1}/{total} selesai. Mengaudit cabang splitter & panjang kabel..."
            save_job_state(job_id, state)
            return jsonify({
                'status': 'processing',
                'step': state["step"]
            })

        else:
            verified = run_audit_pass(
                b64_img=b64_img
