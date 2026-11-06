from flask import Flask, render_template, request, jsonify
import cv2
import pandas as pd
import pytesseract
import re
import os
import io
import numpy as np

app = Flask(__name__)

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
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # 1. Deteksi Warna Cyan/Biru Simbol Antenna Omni
        lower_cyan = np.array([80, 100, 100])
        upper_cyan = np.array([100, 255, 255])
        mask = cv2.inRange(hsv, lower_cyan, upper_cyan)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        antennas = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if 50 < area < 3000:
                M = cv2.moments(cnt)
                if M["m00"] != 0:
                    cX = int(M["m10"] / M["m00"])
                    cY = int(M["m01"] / M["m00"])
                    # Filter area kanan (tempat simbol antena berada)
                    if cX > width * 0.5: 
                        antennas.append({"x": cX, "y": cY})

        # Fallback jika tidak terdeteksi cyan
        if not antennas:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            ocr_data = pytesseract.image_to_data(gray, config=r'--oem 3 --psm 6', output_type=pytesseract.Output.DATAFRAME)
            ocr_data = ocr_data[ocr_data.text.notnull()]
            for idx, row in ocr_data.iterrows():
                text = str(row['text']).strip().upper()
                if re.search(r'A[O0P]\s*[-_]?\s*\d+', text):
                    antennas.append({"x": row['left'], "y": row['top']})

        # Urutkan posisi antena dari atas ke bawah
        antennas = sorted(antennas, key=lambda a: a['y'])

        # 2. Cari Teks LANTAI untuk Menentukan Garis Pembatas (split_y)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ocr_data = pytesseract.image_to_data(gray, config=r'--oem 3 --psm 6', output_type=pytesseract.Output.DATAFRAME)
        ocr_data = ocr_data[ocr_data.text.notnull()]

        y_lantai2 = None
        for idx, row in ocr_data.iterrows():
            text = str(row['text']).strip().upper()
            if 'LANTAI' in text and '2' in text:
                y_lantai2 = row['top']
                break

        # Jika teks LANTAI 2 terdeteksi, garis pembatas berada tepat di bawah posisi Y teks tersebut
        if y_lantai2 is not None:
            split_y = y_lantai2 + 30
        else:
            # Batas toleransi presisi jika OCR teks tidak terbaca (sekitar pertengahan tinggi)
            split_y = height * 0.53

        top_floor = "LANTAI 2"
        bottom_floor = "LANTAI 1"

        extracted_rows = []
        total_antennas = len(antennas)
        
        for idx, ant in enumerate(antennas, 1):
            posisi_lantai = top_floor if ant['y'] < split_y else bottom_floor
            ant_name = f"AO-{total_antennas - idx + 1}"

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

        df = pd.DataFrame(extracted_rows)

        return jsonify({
            'success': True,
            'count': len(df),
            'data': df.to_dict(orient='records')
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
