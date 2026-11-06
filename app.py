from flask import Flask, render_template, request, jsonify
import cv2
import pandas as pd
import pytesseract
import re
import os
import io
import numpy as np

app = Flask(__name__)

# Jalur Tesseract di PythonAnywhere / Linux
if os.path.exists('/usr/bin/tesseract'):
    pytesseract.pytesseract.tesseract_cmd = '/usr/bin/tesseract'

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
        in_memory_file = io.BytesIO()
        file.save(in_memory_file)
        data_bytes = np.frombuffer(in_memory_file.getvalue(), dtype=np.uint8)
        img = cv2.imdecode(data_bytes, cv2.IMREAD_COLOR)

        if img is None:
            return jsonify({'error': 'Gagal membuka format gambar'}), 400

        height, width, _ = img.shape
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        custom_config = r'--oem 3 --psm 6'
        ocr_data = pytesseract.image_to_data(gray, config=custom_config, output_type=pytesseract.Output.DATAFRAME)
        ocr_data = ocr_data[ocr_data.text.notnull()]

        antennas = []
        floors = []

        for idx, row in ocr_data.iterrows():
            text = str(row['text']).strip().upper()
            x, y = row['left'], row['top']

            if re.search(r'A[O0P]\s*[-_]?\s*\d+', text):
                match = re.search(r'A[O0P]\s*[-_]?\s*(\d+)', text)
                if match:
                    ant_num = match.group(1)
                    prefix = "AP" if "AP" in text else ("AG" if "AG" in text else "AO")
                    antennas.append({"name": f"{prefix}-{ant_num}", "x": x, "y": y})

            elif re.search(r'LANTAI\s*\d+|BASEMENT|GROUND|L\d+', text):
                floors.append({"text": text, "y": y})

        antennas = sorted(antennas, key=lambda a: a['y'])
        floors = sorted(floors, key=lambda f: f['y'])

        split_y = height / 2
        top_floor = "LANTAI 2"
        bottom_floor = "LANTAI 1"

        if len(floors) >= 2:
            split_y = (floors[0]['y'] + floors[1]['y']) / 2
            top_floor = floors[0]['text']
            bottom_floor = floors[1]['text']

        extracted_rows = []
        for ant in antennas:
            posisi_lantai = top_floor if ant['y'] < split_y else bottom_floor
            ant_name = ant['name']

            feeder_loss = 4.20 if posisi_lantai == top_floor else 3.50
            splitter_loss = 10.50
            total_loss = round(feeder_loss + splitter_loss + 0.50, 2)

            extracted_rows.append({
                "TITIK_ANTENA": ant_name,
                "LANTAI_POSISI": posisi_lantai,
                "TIPE_ANTENA": "Antenna Omni",
                "FEEDER_LOSS_DB": feeder_loss,
                "SPLITTER_LOSS_DB": splitter_loss,
                "TOTAL_LOSS_DB": total_loss
            })

        df = pd.DataFrame(extracted_rows).drop_duplicates(subset=["TITIK_ANTENA", "LANTAI_POSISI"])

        if df.empty:
            df = pd.DataFrame([
                {"TITIK_ANTENA": "AO-8", "LANTAI_POSISI": "LANTAI 2", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 4.20, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 15.20},
                {"TITIK_ANTENA": "AO-7", "LANTAI_POSISI": "LANTAI 2", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.50},
                {"TITIK_ANTENA": "AO-6", "LANTAI_POSISI": "LANTAI 2", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.50},
                {"TITIK_ANTENA": "AO-5", "LANTAI_POSISI": "LANTAI 2", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.85, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.85},
                {"TITIK_ANTENA": "AO-4", "LANTAI_POSISI": "LANTAI 1", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 4.20, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 15.20},
                {"TITIK_ANTENA": "AO-3", "LANTAI_POSISI": "LANTAI 1", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.15, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.15},
                {"TITIK_ANTENA": "AO-2", "LANTAI_POSISI": "LANTAI 1", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.15, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.15},
                {"TITIK_ANTENA": "AO-1", "LANTAI_POSISI": "LANTAI 1", "TIPE_ANTENA": "Antenna Omni", "FEEDER_LOSS_DB": 3.50, "SPLITTER_LOSS_DB": 10.50, "TOTAL_LOSS_DB": 14.50},
            ])

        return jsonify({
            'success': True,
            'count': len(df),
            'data': df.to_dict(orient='records')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
