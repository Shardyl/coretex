#!/usr/bin/env bash
# Build the signed release APK on the Cortex box. Run as root from /opt/health-bridge-build.
# Secrets are read from /etc/cortex at build time and never written into the source tree.
set -euo pipefail
cd "$(dirname "$0")"

export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}
export ANDROID_HOME=${ANDROID_HOME:-/opt/android-sdk}
export ANDROID_SDK_ROOT=$ANDROID_HOME
KS=/etc/cortex/health-bridge.keystore
KSP=/etc/cortex/health-bridge.keystore.properties

# One-time keystore creation (random passwords, root-only).
if [ ! -f "$KS" ]; then
  umask 077
  PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)
  "$JAVA_HOME/bin/keytool" -genkeypair -v -keystore "$KS" -storetype PKCS12 \
    -alias healthbridge -keyalg RSA -keysize 4096 -validity 10000 \
    -storepass "$PASS" -keypass "$PASS" \
    -dname "CN=Cortex Health Bridge, O=Sky Vision, C=AE" >/dev/null 2>&1
  printf 'storeFile=%s\nstorePassword=%s\nkeyAlias=healthbridge\nkeyPassword=%s\n' "$KS" "$PASS" "$PASS" > "$KSP"
  chown root:root "$KS" "$KSP"; chmod 600 "$KS" "$KSP"
fi

export HB_KEYSTORE_PROPS=$KSP
export BRIDGE_TOKEN_FILE=/etc/cortex/fitness_bridge_token
echo "sdk.dir=$ANDROID_HOME" > local.properties

./gradlew --no-daemon -q clean assembleRelease

mkdir -p out
cp app/build/outputs/apk/release/app-release.apk out/cortex-health-bridge.apk
"$ANDROID_HOME/build-tools/36.0.0/apksigner" verify --print-certs out/cortex-health-bridge.apk | grep -E 'SHA-256' || true
echo "APK: $(pwd)/out/cortex-health-bridge.apk"
