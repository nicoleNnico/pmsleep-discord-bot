from PIL import Image
import os

# 設定你的圖片資料夾路徑
image_dir = "assets/images"

for filename in os.listdir(image_dir):
    # 檢查是否為常見的圖片格式
    if filename.lower().endswith((".png", ".jpg", ".jpeg")):
        img_path = os.path.join(image_dir, filename)

        # 開啟圖片
        with Image.open(img_path) as img:
            # 取得不包含副檔名的檔名
            base_name = os.path.splitext(filename)[0]
            output_path = os.path.join(image_dir, f"{base_name}.webp")

            # 儲存為 webp 格式（quality 可設定 80~90，畫質好且檔案極小）
            img.save(output_path, "WEBP", quality=85)
            print(f"已成功轉換：{filename} -> {base_name}.webp")