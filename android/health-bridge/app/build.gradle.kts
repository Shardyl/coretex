import java.io.File
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.parcelize")
}

// Token: injected at build time only, never committed.
// Source order: -PbridgeToken, env BRIDGE_TOKEN, file named by env BRIDGE_TOKEN_FILE.
fun bridgeToken(): String {
    (project.findProperty("bridgeToken") as String?)?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
    System.getenv("BRIDGE_TOKEN")?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
    System.getenv("BRIDGE_TOKEN_FILE")?.let { f ->
        val file = File(f)
        if (file.canRead()) return file.readText().lineSequence().firstOrNull()?.trim().orEmpty()
    }
    return ""
}

// Signing: properties file path from env HB_KEYSTORE_PROPS (storeFile, storePassword, keyAlias, keyPassword).
val signingProps: Properties? = System.getenv("HB_KEYSTORE_PROPS")?.let { p ->
    val f = File(p)
    if (f.canRead()) Properties().apply { f.inputStream().use { load(it) } } else null
}

android {
    namespace = "uk.coretex.healthbridge"
    compileSdk = 36

    defaultConfig {
        applicationId = "uk.coretex.healthbridge"
        minSdk = 29 // Samsung Health Data SDK needs API 29+
        targetSdk = 35
        versionCode = 6
        versionName = "1.3.1"
        val escaped = bridgeToken().replace("\\", "\\\\").replace("\"", "\\\"")
        buildConfigField("String", "BRIDGE_TOKEN", "\"$escaped\"")
        buildConfigField("String", "API_URL", "\"https://coretex.uk/api/fitness/health\"")
    }

    signingConfigs {
        if (signingProps != null) {
            create("release") {
                storeFile = File(signingProps.getProperty("storeFile"))
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (signingProps != null) signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("androidx.health.connect:connect-client:1.1.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")

    // Samsung Health Data SDK 1.1.0. Its licence forbids redistribution: the AAR is NEVER committed.
    // build-on-box.sh copies it from /opt/samsung-health-sdk/ into app/libs/ at build time.
    implementation(files("libs/samsung-health-data-api-1.1.0.aar"))
    implementation("com.google.code.gson:gson:2.11.0")
}
