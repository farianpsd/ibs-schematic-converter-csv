import streamlit as st
import cv2
import numpy as np
import pandas as pd
from PIL import Image
import easyocr
import re

st.set_page_config(page_title="IBS Schematic to CSV Converter", page_icon="🖼️", layout="centered")

st.title("🖼️ IBS Schematic to CSV Converter")
st.write("Convert diagram skematik IBS secara presisi & 100% dinamis berbasis analisis spasial OCR.")

# CSS Tampilan Clean
st.markdown("""
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def load_ocr():
    return easyocr.Reader(['en'], gpu=False)

reader = load_ocr()

uploaded_file = st.file_uploader("Upload Gambar Skematik (.png / .jpg)", type=["png", "jpg", "jpeg"])

if uploaded_file is not None:
    file_bytes = np.asarray(bytearray(uploaded_file.read()), dtype=np.uint8)
    img_cv = cv2.imdecode(file_bytes, 1)
    height, width, _ = img_cv.shape
    
    image_pil = Image.open(uploaded_file)
    st.image(image_pil, caption="Preview Skematik Terunggah", use_container_width=True)
    
    if st.button("⚡ Proses Konversi ke CSV", type="primary"):
        with st.spinner("Menganalisis skematik & membaca data spasial dinamis..."):
            
            # Preprocessing ringan untuk mempertajam kontras font tanpa membebani RAM
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
            enhanced = clahe.apply(gray)
            
            # OCR Pas Tunggal pada Gambar Ter-enhance
            results = reader.readtext(enhanced)

            floor_labels = []
            antennas_found = []
            feeder_texts = []
            splitter_types = []

            for (bbox, text, prob) in results:
                clean_t = text.strip().upper().replace(" ", "")
                center_y = (bbox[0][1] + bbox[2][1]) / 2
                center_x = (bbox[0][0] + bbox[2][0]) / 2
                
                # 1. Deteksi Label Lantai Dinamis (LANTAI 1, 2, 3, GF, BASEMENT, dll)
                if re.search(r'LANTAI\s*\d+|BASEMENT|GROUND|GF|L\d+', clean_t):
                    floor_labels.append({"text": text.strip().upper(), "y": center_y})
                
                # 2. Deteksi Kodifikasi Antena Dinamis (AO-1..AO-99, AP-1, AG-1, dsb)
                elif re.search(r'A[O0P]\s*[-_]?\s*\d+', clean_t):
                    match = re.search(r'A[O0P]\s*[-_]?\s*(\d+)', clean_t)
                    if match:
                        ant_num = match.group(1)
                        prefix = "AP" if "AP" in clean_t else ("AG" if "AG" in clean_t else "AO")
                        antennas_found.append({"name": f"{prefix}-{ant_num}", "x": center_x, "y": center_y})
                
                # 3. Deteksi Teks Feeder Meteran (misal: 20 METER, 25 METER, 30 METER, 35 METER)
                elif "METER" in clean_t or "MTR" in clean_t:
                    match = re.search(r'(\d+)\s*(METER|MTR|M)', clean_t)
                    if match:
                        feeder_texts.append({"meter": float(match.group(1)), "y": center_y, "x": center_x})

                # 4. Deteksi Tipe Splitter (2 WAY, 3 WAY, 4 WAY)
                elif "WAY" in clean_t or "SPLITER" in clean_t or "SPLITTER" in clean_t:
                    if "2" in clean_t:
                        splitter_types.append({"loss": 3.50, "y": center_y})
                    elif "3" in clean_t:
                        splitter_types.append({"loss": 5.50, "y": center_y})
                    elif "4" in clean_t:
                        splitter_types.append({"loss": 7.00, "y": center_y})

            # Filter Duplikat Hasil OCR Antena
            unique_antennas = []
            for ant in antennas_found:
                if not any(u['name'] == ant['name'] and abs(u['y'] - ant['y']) < (height * 0.08) for u in unique_antennas):
                    unique_antennas.append(ant)

            antennas_found = sorted(unique_antennas, key=lambda a: a['y'])

            # Urutkan Label Lantai dari Atas ke Bawah
            floor_labels = sorted(floor_labels, key=lambda f: f['y'])

            data_extracted = []
            
            for ant in antennas_found:
                # Tentukan Nama Lantai Berdasarkan Koordinat Y Terdekat
                posisi_lantai = "LANTAI UTAMA"
                if floor_labels:
                    closest_floor = min(floor_labels, key=lambda f: abs(f['y'] - ant['y']))
                    posisi_lantai = closest_floor['text']

                # Cari Panjang Kabel Feeder Cabang Terdekat Sumbu Y
                branch_meter = 20.0
                if feeder_texts:
                    closest_feeder = min(feeder_texts, key=lambda f: abs(f['y'] - ant['y']))
                    branch_meter = closest_feeder['meter']

                # Hitung Redaman Kabel Feeder 1/2" (0.07 dB/m)
                cable_loss = round(branch_meter * 0.07, 2)

                # Hitung Redaman Splitter Bertingkat Dinamis
                # Default: Splitter Utama 2-Way (3.5 dB) + Splitter Cabang 4-Way (7.0 dB) = 10.5 dB
                splitter_loss = 10.50
                if splitter_types:
                    # Ambil splitter cabang terdekat
                    closest_spl = min(splitter_types, key=lambda s: abs(s['y'] - ant['y']))
                    splitter_loss = 3.50 + closest_spl['loss']

                connector_loss = 0.50
                total_loss = round(cable_loss + splitter_loss + connector_loss, 2)
                
                tipe_ant = "Antenna Planar" if "AP" in ant['name'] else ("Antenna High Gain" if "AG" in ant['name'] else "Antenna Omni")

                data_extracted.append({
                    "TITIK_ANTENA": ant['name'],
                    "LANTAI_POSISI": posisi_lantai,
                    "TIPE_ANTENA": tipe_ant,
                    "FEEDER_LOSS_DB": cable_loss,
                    "SPLITTER_LOSS_DB": splitter_loss,
                    "TOTAL_LOSS_DB": total_loss
                })

            df = pd.DataFrame(data_extracted)
            
            # Tampilkan Hasil Murni dari Gambar Skematik
            if not df.empty:
                st.success(f"✅ Deteksi Dinamis Berhasil! Ditemukan {len(df)} titik antena terdeteksi.")
                st.dataframe(df, use_container_width=True)
                
                csv_bytes = df.to_csv(index=False).encode('utf-8')
                st.download_button(
                    label="📥 Download File CSV Clean",
                    data=csv_bytes,
                    file_name="Link_Budget_Skematik_Dynamic.csv",
                    mime="text/csv"
                )
            else:
                st.error("⚠️ Teks tidak terbaca. Pastikan resolusi gambar jelas dan coba upload ulang.")
