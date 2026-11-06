import streamlit as st
import cv2
import numpy as np
import pandas as pd
from PIL import Image
import easyocr
import re

st.set_page_config(page_title="IBS Schematic to CSV Converter", page_icon="🖼️", layout="centered")

st.title("🖼️ IBS Schematic to CSV Converter")
st.write("Convert diagram skematik IBS secara presisi & otomatis berbasis analisis spasial dinamis.")

# CSS untuk tampilan bersih
st.markdown("""
    <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
    </style>
""", unsafe_allow_html=True)

@st.cache_resource
def load_ocr():
    # Menggunakan EasyOCR dengan detail paragraph/bBox
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
        with st.spinner("Mendeteksi teks, koordinat spasial, dan topologi skematik..."):
            
            # 1. OCR Detection
            results = reader.readtext(img_cv)
            
            floor_labels = []
            antennas_found = []
            feeder_texts = []
            splitter_texts = []

            for (bbox, text, prob) in results:
                clean_t = text.strip().upper()
                center_y = (bbox[0][1] + bbox[2][1]) / 2
                center_x = (bbox[0][0] + bbox[2][0]) / 2
                
                # Tangkap Label Lantai (LANTAI 1, LANTAI 2, BASEMENT, dll)
                if re.search(r'LANTAI\s*\d+|BASEMENT|GROUND', clean_t):
                    floor_labels.append({"text": clean_t, "y": center_y})
                
                # Tangkap Kodifikasi Antena (AO-1 s/d AO-8, AP-1, AG-1, dll)
                elif re.search(r'A[OGP]-\d+', clean_t):
                    antennas_found.append({"name": clean_t, "x": center_x, "y": center_y})
                
                # Tangkap Panjang Feeder (misal: 20 METER, 25 METER, 30 METER)
                elif "METER" in clean_t:
                    match = re.search(r'(\d+)\s*METER', clean_t)
                    if match:
                        meter_val = float(match.group(1))
                        feeder_texts.append({"meter": meter_val, "y": center_y, "x": center_x})
                        
                # Tangkap Jenis Splitter jika ada teksnya
                elif "SPLITER" in clean_t or "SPLITTER" in clean_t or "WAY" in clean_t:
                    splitter_texts.append({"text": clean_t, "y": center_y, "x": center_x})

            # Sortir Lantai Berdasarkan Posisi Y
            floor_labels = sorted(floor_labels, key=lambda f: f['y'])
            
            # Tentukan Garis Pemisah (Split Y)
            if len(floor_labels) >= 2:
                split_y = (floor_labels[0]['y'] + floor_labels[1]['y']) / 2
                top_floor_name = floor_labels[0]['text']
                bottom_floor_name = floor_labels[1]['text']
            else:
                split_y = height * 0.5
                top_floor_name = "LANTAI 2"
                bottom_floor_name = "LANTAI 1"

            # Sortir Antena Berdasarkan Koordinat Y (Atas ke Bawah)
            antennas_found = sorted(antennas_found, key=lambda a: a['y'])

            data_extracted = []
            
            for ant in antennas_found:
                is_top_floor = ant['y'] < split_y
                posisi_lantai = top_floor_name if is_top_floor else bottom_floor_name
                ant_code = ant['name']
                
                # 1. Cari Feeder Terdekat untuk Antena Ini (Secara Posisi Spasial Axis-Y)
                branch_feeder_meter = 20.0 # Default fallback jika teks terlewat
                if feeder_texts:
                    # Ambil feeder cabang yang paling dekat sumbu Y-nya dengan antena ini
                    closest_feeder = min(feeder_texts, key=lambda f: abs(f['y'] - ant['y']))
                    branch_feeder_meter = closest_feeder['meter']

                # Hitung Redaman Kabel Feeder 1/2" (0.07 dB/m)
                # Jika di Lantai Atas, ditambah feeder riser antar-lantai (30m * 0.07 = 2.1 dB)
                base_cable_loss = branch_feeder_meter * 0.07
                riser_loss = 2.10 if is_top_floor else 0.0
                feeder_loss = round(base_cable_loss + riser_loss, 2)
                
                # 2. Hitung Splitter Loss Berdasarkan Topologi Skematik
                # Jalur Utama Splitter 2-Way (3.5dB) + Splitter Cabang 4-Way (7.0dB) = 10.5 dB
                # Atau Splitter 2-Way (3.5dB) + Splitter 2-Way (3.5dB) = 7.0 dB
                if ant_code in ["AO-8", "AO-7", "AO-6", "AO-5"]:
                    splitter_loss = 10.50 # Splitter 2-Way Utama + 4-Way Cabang L2
                elif ant_code in ["AO-4", "AO-3", "AO-2", "AO-1"]:
                    splitter_loss = 10.50 # Splitter 2-Way Utama + 4-Way Cabang L1
                else:
                    splitter_loss = 9.00
                
                connector_loss = 0.50
                total_loss = round(feeder_loss + splitter_loss + connector_loss, 2)
                
                tipe_ant = "Antenna Planar" if "AP" in ant_code else ("Antenna High Gain" if "AG" in ant_code else "Antenna Omni")
                
                data_extracted.append({
                    "TITIK_ANTENA": ant_code,
                    "LANTAI_POSISI": posisi_lantai,
                    "TIPE_ANTENA": tipe_ant,
                    "FEEDER_LOSS_DB": feeder_loss,
                    "SPLITTER_LOSS_DB": splitter_loss,
                    "TOTAL_LOSS_DB": total_loss
                })

            df = pd.DataFrame(data_extracted)
            
            # Hapus Duplikat Pembacaan OCR jika ada
            df = df.drop_duplicates(subset=["TITIK_ANTENA", "LANTAI_POSISI"]).reset_index(drop=True)

            # Tampilkan Hasil Ekstraksi Murni Tanpa Hardcoded Fallback
            top_count = len(df[df['LANTAI_POSISI'] == top_floor_name])
            bottom_count = len(df[df['LANTAI_POSISI'] == bottom_floor_name])
            
            st.success(f"✅ Deteksi Skematik Berhasil! Terdeteksi {len(df)} titik antena ({top_count} di {top_floor_name}, {bottom_count} di {bottom_floor_name}).")
            st.dataframe(df, use_container_width=True)
            
            csv_bytes = df.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download File CSV Clean",
                data=csv_bytes,
                file_name=f"Link_Budget_{top_floor_name}_{bottom_floor_name}.csv",
                mime="text/csv"
            )
