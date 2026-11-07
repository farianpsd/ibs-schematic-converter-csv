from flask import Flask, render_template, request, jsonify
import os
import json
import base64
import requests

app = Flask(__name__)

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

        # PROMPT MURNI GENERIK, FLEKSIBEL & DINAMIS (TANPA HARDCODE CONTOH)
        system_instruction = "Kamu adalah parser skematik IBS. Tugasmu mendeteksi 100% seluruh terminal antena pada diagram tanpa melewatkan satu pun."
        
        user_prompt = """
        Analisis gambar skematik IBS ini dan ekstrak seluruh titik antena distribusi secara dinamis.

        PENANGANAN AREA & ELEMEN PENTING:
        - Abaikan Antenna Donor (di area luar/kiri) dan area tabel informasi di sebelah kanan.
        - Fokus pada seluruh simbol antena (lingkaran atau segitiga warna Cyan/Biru) yang terhubung di ujung kabel Feeder 1/2".

        LANGKAH EKSTRAKSI MANDATORI:
        1. DETEKSI SEMUA ANTENA:
           - Pindai dari bawah ke atas pada diagram.
           - Setiap ikon cyan di ujung kanan garis kabel ADALAH 1 TITIK ANTENA TERPISAH.
           - Beri penamaan TITIK_ANTENA secara berurutan: AO-1, AO-2, AO-3, dst.

        2. LOKASI LANTAI (LANTAI_POSISI):
           - Baca teks nama lantai pada zona horizontal tempat antena berada (misal: LANTAI 1, LANTAI 2, BASEMENT, dsb.).

        3. TIPE ANTENA (TIPE_ANTENA):
           - "Antenna Omni" jika berbentuk lingkaran Cyan/Biru.
           - "Antenna Planar" jika berbentuk segitiga Cyan/Biru.

        4. KABEL_LIST:
           - Catat semua nilai panjang kabel Feeder 1/2" (angka diikuti METER) yang dilewati jalur sinyal dari Repeater menuju titik antena tersebut dalam bentuk list/array angka (misal: [20, 15, 25]).

        5. SPLITTER_LIST:
           - Catat semua komponen pembagi sinyal (Splitter 2-Way, 3-Way, 4-Way, Coupler) yang berada di sepanjang jalur sinyal menuju antena tersebut dalam bentuk list/array string (misal: ["2-Way", "3-Way"]).

        Format keluaran WAJIB berupa JSON murni array of objects:
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

        payload = {
            "model": "qwen/qwen3.8-27b",
            "messages": [
                {
                    "role": "system",
                    "content": system_instruction
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"}
        }

        response = requests.post(url, json=payload, headers=headers, timeout=120)
        res_json = response.json()

        if response.status_code != 200:
            error_msg = res_json.get('error', {}).get('message', 'API Error')
            return jsonify({'error': f"API Error: {error_msg}"}), 400

        raw_text = res_json['choices'][0]['message']['content']
        parsed_data = json.loads(raw_text)

        if isinstance(parsed_data, dict):
            for key in parsed_data:
                if isinstance(parsed_data[key], list):
                    parsed_data = parsed_data[key]
                    break

        # LOGIKA PERHITUNGAN PYTHON (DINAMIS MURNI)
        final_results = []
        for item in parsed_data:
            kabel_list = item.get('KABEL_LIST', [])
            splitters = item.get('SPLITTER_LIST', [])

            # 1. Total Kabel Meter * 0.07 dB/m
            total_m = 0.0
            if isinstance(kabel_list, list):
                for c in kabel_list:
                    try:
                        total_m += float(c)
                    except (ValueError, TypeError):
                        pass

            feeder_loss = round(total_m * 0.07, 2)

            # 2. Total Splitter Loss
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

            # 3. Total Loss (+ 0.5 connector/jumper margin)
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
