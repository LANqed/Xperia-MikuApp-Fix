#!/usr/bin/env sh
# 一键改机：把 apk/ 里的四个 Miku 应用指向你自己的服务端并签名（Linux/macOS）。
# 用法（在本目录执行）：
#   bash patch-apk.sh                               # 默认 http://192.168.31.104:8080
#   bash patch-apk.sh http://miku.example.com
#   bash patch-apk.sh http://miku.example.com noslib   # 未刷联动包时去掉 uses-library
# 产物：本目录 patched/ 下的四个 apk，直接 adb install。
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
BASE_URL="${1:-http://192.168.31.104:8080}"
NO_SHARED_LIB="${2:-}"
BASE_URL="${BASE_URL%/}"

case "$BASE_URL" in
  http://*|https://*) ;;
  *) echo "错误：BaseUrl 必须以 http:// 或 https:// 开头"; exit 1 ;;
esac

command -v java >/dev/null 2>&1 || { echo "错误：找不到 java，请先安装 JDK 8+ 并加入 PATH"; exit 1; }
for j in apktool_2.9.3.jar uber-apk-signer-1.3.0.jar; do
  [ -f "$ROOT/$j" ] || { echo "错误：缺少 $j，请把它放到本目录"; exit 1; }
done

WORK="$ROOT/apk-work"
OUT="$WORK/out"
PATCHED="$ROOT/patched"
rm -rf "$WORK" "$PATCHED"
mkdir -p "$OUT" "$PATCHED"

OLD_HOSTS='http://distribute.mikuxperia.com http://auth.mikuxperia.com http://evawdt.otenki.co.jp'

patch_one() {
  APK="$1"; DIR="$2"
  echo
  echo "==> [1/3] 解包 $APK"
  java -jar "$ROOT/apktool_2.9.3.jar" d -f -o "$WORK/$DIR" "$ROOT/apk/$APK" >/dev/null

  echo "==> [2/3] 替换域名 -> $BASE_URL"
  for host in $OLD_HOSTS; do
    find "$WORK/$DIR" -name '*.smali' -type f -print0 \
      | xargs -0 sed -i "s#$host#$BASE_URL#g"
  done

  if [ -n "$NO_SHARED_LIB" ]; then
    echo "==>      删除 <uses-library> 依赖（noslib）"
    sed -i '/<uses-library android:name="com.mikuxperia.mikuxperia_library"/d' "$WORK/$DIR/AndroidManifest.xml"
  fi

  echo "==> [3/3] 重打包 $APK"
  OUT_APK="$OUT/${APK%.apk}-unsigned.apk"
  java -jar "$ROOT/apktool_2.9.3.jar" b "$WORK/$DIR" -o "$OUT_APK" >/dev/null
}

patch_one com.mikuxperia.featuresongsplayerapp.apk featuresongs
patch_one com.mikuxperia.mikunewsapp.apk           mikunews
patch_one com.mikuxperia.mikuweatherwidget.apk     mikuweather
patch_one MikuDownloader.apk                       downloader

echo
echo "==> 签名"
java -jar "$ROOT/uber-apk-signer-1.3.0.jar" --apks "$OUT" --allowResign >/dev/null

echo
echo "==> 收拢成品"
for f in "$OUT"/*-aligned-debugSigned.apk; do
  name="$(basename "$f")"
  name="${name%-aligned-debugSigned.apk}.apk"
  cp "$f" "$PATCHED/$name"
done

echo
echo "完成！成品在 patched/ 目录："
ls -1 "$PATCHED"
echo
echo "安装到设备（设备上已有旧版请先 adb uninstall 同名包）："
echo "  adb install patched/<上面的文件名>"
