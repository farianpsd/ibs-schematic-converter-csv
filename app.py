from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

# API Key Groq Anda
GROQ_API_KEY = "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"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
def convert():
    if 'file' not in request.files:
        return jsonify({'error': 'File tidak ditemukan'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'File belum dipilih'}), 400

    try:
        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        mime_type = file.mimetype or 'image/png'

        # PROMPT OPTIMASI FOKUS DETEKSI JUMLAH ANTENA (DINAMIS UNTUK SEGALA GAMBAR)
        prompt = """
        Kamu adalah sistem Vision OCR pakar analisis skematik IBS (In-Building System).
        Tugas utama: Deteksi dan ekstrak SELURUH titik antena distribusi tanpa ada satu pun yang terlewat.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor di sebelah kiri (beserta kabel Feeder 7/8" 70m).
        - Abaikan Title Block, Legenda REMARKS, dan Tabel Approval/Revisi di sisi paling kanan.

        INSTRUKSI DETEKSI ANTENA (SANGAT KETAT):
        1. CARI SEMUA TITIK ANTENA: Antena selalu berada di UJUNG KANAN dari garis kabel Feeder 1/2". Antena ditandai dengan ikon Lingkaran atau Segitiga berwarna Cyan/Biru Terang.
        2. TELUSURI SELURUH PERCABANGAN: Pindai diagram dari ujung bawah hingga ujung atas. Periksa setiap percabangan Splitter, pastikan setiap cabang yang berujung pada ikon Cyan/Biru tercatat sebagai 1 titik antena terpisah.
        3. PENAMAAN (TITIK_ANTENA): Urutkan penamaan dari antena posisi paling bawah ke paling atas pada gambar (AO-1, AO-2, AO-3, AO-4, dst.).
        4. LANTAI_POSISI: Identifikasi nama lantai tempat antena dipasang dari label area terdekat (misal: LANTAI 1, LANTAI 2, BASEMENT, dsb.).
        5. TIPE_ANTENA: 
           - Lingkaran Cyan/Biru = "Antenna Omni"
           - Segitiga Cyan/Biru = "Antenna Planar"
        
        6. KABEL_LIST: Catat seluruh angka panjang kabel Feeder 1/2" (dalam Meter) yang ada di sepanjang jalur dari Repeater ke antena tersebut sebagai array angka (misal: [20, 15, 25]).
        7. SPLITTER_LIST: Catat seluruh jenis pembagi sinyal yang dilewati dari Repeater ke antena tersebut sebagai array string (misal: ["2-Way", "2-Way", "3-Way"]).

        Kembalikan HANYA format JSON murni array of objects tanpa teks/markdown tambahan:
        [
          {
            "TITIK_ANTENA": "AO-1",
            "LANTAI_POSISI": "LANTAI 1",
            "TIPE_ANTENA": "Antenna Omni",
            "KABEL_LIST": [20, 15, 30],
            "SPLITTER_LIST": ["2-Way", "2-Way", "3-Way"]
          }
        ]
        """

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        # Model Vision tetap qwen/qwen3.8-27b
        VISION_MODELS = [
            "qwen/qwen3.8-27b"
        ]

        res_json = None
        last_error = ""

        for vision_model in VISION_MODELS:
            payload = {
                "model": vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                "temperature": 0.0,  # Memastikan deteksi konsisten & deterministik
                "response_format": {"type": "json_object"}
            }

            response = requests.post(url, json=payload, headers=headers, timeout=120)
            res_json = response.json()

            if response.status_code == 200:
                break
            else:
                last_error = res_json.get('error', {}).get('message', 'API Error')

        if not res_json or 'choices' not in res_json:
            return jsonify({'error': f"Gagal memproses gambar: {last_error}"}), 400

        raw_text = res_json['choices'][0]['message']['content']
        parsed_data = json.loads(raw_text)
        
        # Ekstrak array jika JSON dibungkus oleh objek utama
        if isinstance(parsed_data, dict):
            for key in parsed_data:
                if isinstance(parsed_data[key], list):
                    parsed_data = parsed_data[key]
                    break

        # LOGIKA KALKULASI MATEMATIS PYTHON
        final_results = []
        for item in parsed_data:
            kabel_list = item.get('KABEL_LIST', [])
            splitters = item.get('SPLITTER_LIST', [])

            # 1. Total Feeder Loss = Sum(Seluruh Segmen Kabel 1/2") * 0.07 dB/m
            total_panjang = 0.0
            if isinstance(kabel_list, list):
                for val in kabel_list:
                    try:
                        total_panjang += float(val)
                    except (ValueError, TypeError):
                        pass

            feeder_loss = round(total_panjang * 0.07, 2)

            # 2. Total Splitter Loss = Sum(Seluruh Splitter yang Dilewati)
            splitter_loss = 0.0
            if isinstance(splitters, list):
                for s in splitters:
                    s_str = str(s).lower()
                    if '2' in s_str:
                        splitter_loss += 3.5
                    elif '3' in s_str:
                        splitter_loss += 6.0
                    elif '4' in s_str:
                        splitter_loss += 7.0

            splitter_loss = round(splitter_loss, 2)

            # 3. Total Loss = Feeder Loss + Splitter Loss + 0.5 dB
            total_loss = round(feeder_loss + splitter_loss + 0.5, 2)

            final_results.append({
                "TITIK_ANTENA": item.get("TITIK_ANTENA"),
                "LANTAI_POSISI": item.get("LANTAI_POSISI"),
                "TIPE_ANTENA": item.get("TIPE_ANTENA"),
                "FEEDER_LOSS_DB": feeder_loss,
                "SPLITTER_LOSS_DB": splitter_loss,
                "TOTAL_LOSS_DB": total_loss
            })

        return jsonify({
            'success': True,
            'count': len(final_results),
            'data': final_results
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
