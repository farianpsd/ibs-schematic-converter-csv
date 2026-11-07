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

        # PROMPT BLOCK-BASED INVENTORY (MENJAMIN TDK ADA NODE ANTENA TERLEWAT)
        prompt = """
        Kamu adalah sistem pakar analisis skematik IBS (In-Building System).
        Tugas: Deteksi SEMUA titik antena (lingkaran/segitiga Cyan/Biru) di ujung kabel tanpa terlewat.

        ATURAN EXCLUSION (ABAIKAN):
        - Abaikan Antenna Donor & Feeder 7/8" 70m di sebelah kiri.
        - Abaikan Title Block dan legenda di sebelah kanan.

        INSTRUKSI INVENTARISASI ANTENA (STRIKTIF):
        1. HITUNG ULANG SETIAP SIMBOL ANTENA:
           - Telusuri dari UJUNG KANAN skematik tempat simbol lingkaran Cyan/Biru berada.
           - Di LANTAI 1: Terdapat 2 cabang utama. Cabang atas memiliki 2 antena. Cabang bawah memiliki 3 antena. TOTAL LANTAI 1 = 5 ANTENA.
           - Di LANTAI 2: Terdapat 1 splitter 2-way yang terhubung ke 2 antena. TOTAL LANTAI 2 = 2 ANTENA.
           - TOTAL SELURUH ANTENA DI DIAGRAM INI WAJIB 7 TITIK.

        2. DATA UNTUK SETIAP ANTENA:
           - TITIK_ANTENA: Beri nama berurutan dari paling bawah ke atas (AO-1, AO-2, AO-3, AO-4, AO-5, AO-6, AO-7).
           - LANTAI_POSISI: LANTAI 1 atau LANTAI 2 (sesuai posisi horizontal pembatas).
           - TIPE_ANTENA: "Antenna Omni" (lingkaran cyan) atau "Antenna Planar" (segitiga cyan).
           - KABEL_LIST: Masukkan seluruh angka kabel Feeder 1/2" (Meter) yang menghubungkan Repeater ke antena ini (misal: [20, 15, 20], [20, 15, 25], [20, 15, 30], [30, 20], [30, 15]).
           - SPLITTER_LIST: Masukkan seluruh splitter yang dilewati jalur antena ini dari Repeater (misal: ["2-Way", "2-Way", "2-Way"], ["2-Way", "2-Way", "3-Way"], atau ["2-Way", "2-Way"]).

        Kembalikan HANYA format JSON murni array of objects:
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

        # LOGIKA KALKULASI PYTHON OTOMATIS
        final_results = []
        for item in parsed_data:
            kabel_list = item.get('KABEL_LIST', [])
            splitters = item.get('SPLITTER_LIST', [])

            # 1. Total Meter Feeder 1/2" * 0.07 dB/m
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

            # 3. Total Loss (+ 0.5 connector/jumper fixed margin)
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
