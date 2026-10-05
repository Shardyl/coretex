package uk.coretex.healthbridge

import android.content.SharedPreferences
import android.os.Bundle
import android.widget.TextView
import androidx.activity.result.ActivityResultLauncher
import androidx.appcompat.app.AppCompatActivity
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import org.json.JSONObject

class MainActivity : AppCompatActivity() {
    private lateinit var status: TextView
    private lateinit var progress: TextView
    private lateinit var btnGrant: MaterialButton
    private lateinit var btnSamsung: MaterialButton
    private lateinit var btnSync: MaterialButton
    private lateinit var btnBackfill: MaterialButton
    private var running: Job? = null

    private val permLauncher: ActivityResultLauncher<Set<String>> =
        registerForActivityResult(PermissionController.createRequestPermissionResultContract()) {
            refresh()
            startSync(SyncMode.SYNC)
        }

    private val prefsListener = SharedPreferences.OnSharedPreferenceChangeListener { _, _ -> refresh() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        progress = findViewById(R.id.progress)
        btnGrant = findViewById(R.id.btnGrant)
        btnSamsung = findViewById(R.id.btnSamsung)
        btnSync = findViewById(R.id.btnSync)
        btnBackfill = findViewById(R.id.btnBackfill)

        btnGrant.setOnClickListener { requestPermissions() }
        btnSamsung.setOnClickListener { connectSamsung() }
        btnSync.setOnClickListener { startSync(SyncMode.SYNC) }
        btnBackfill.setOnClickListener { startSync(SyncMode.BACKFILL) }

        SyncWorker.schedule(this)
        if (savedInstanceState == null) startSync(SyncMode.SYNC, quietIfNotReady = true)
    }

    override fun onStart() {
        super.onStart()
        Status.prefs(this).registerOnSharedPreferenceChangeListener(prefsListener)
        refresh()
    }

    override fun onStop() {
        Status.prefs(this).unregisterOnSharedPreferenceChangeListener(prefsListener)
        super.onStop()
    }

    private fun sdkAvailable() = HealthConnectClient.getSdkStatus(this) == HealthConnectClient.SDK_AVAILABLE

    private fun requestPermissions() {
        if (!sdkAvailable()) { refresh(); return }
        val client = HealthConnectClient.getOrCreate(this)
        val wanted = Perms.DATA.toMutableSet()
        if (featureAvailable(client, HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND)) wanted += Perms.BACKGROUND
        if (featureAvailable(client, HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_HISTORY)) wanted += Perms.HISTORY
        permLauncher.launch(wanted)
    }

    private fun connectSamsung() {
        if (running?.isActive == true) return
        running = lifecycleScope.launch {
            setBusy(true)
            progress.text = "Connecting to Samsung Health"
            val msg = try {
                SamsungReader.connect(this@MainActivity)
            } catch (e: Throwable) {
                "Samsung Health: ${SamsungReader.describe(e)}"
            }
            Status.note(this@MainActivity, "samsung_note", msg)
            setBusy(false)
            refresh()
        }
    }

    private fun featureAvailable(client: HealthConnectClient, feature: Int): Boolean =
        runCatching {
            client.features.getFeatureStatus(feature) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE
        }.getOrDefault(false)

    private fun startSync(mode: SyncMode, quietIfNotReady: Boolean = false) {
        if (running?.isActive == true) return
        running = lifecycleScope.launch {
            if (quietIfNotReady) {
                if (!SyncRunner.hasToken() || !sdkAvailable()) return@launch
                val granted = HealthConnectClient.getOrCreate(this@MainActivity)
                    .permissionController.getGrantedPermissions()
                if (granted.intersect(Perms.DATA).isEmpty()) return@launch
            }
            setBusy(true)
            if (mode == SyncMode.BACKFILL) progress.text = "Backfill running, keep this screen open"
            SyncRunner.run(this@MainActivity, mode) { msg -> runOnUiThread { progress.text = msg } }
            setBusy(false)
            refresh()
        }
    }

    private fun setBusy(busy: Boolean) {
        btnSync.isEnabled = !busy
        btnBackfill.isEnabled = !busy
        btnGrant.isEnabled = !busy
        btnSamsung.isEnabled = !busy
        if (!busy) progress.text = ""
    }

    private fun refresh() {
        lifecycleScope.launch {
            val lines = mutableListOf<String>()
            val sdk = HealthConnectClient.getSdkStatus(this@MainActivity)
            lines += "Health Connect: " + when (sdk) {
                HealthConnectClient.SDK_AVAILABLE -> "available"
                HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> "update required (update Health Connect in Play Store)"
                else -> "not available on this device"
            }
            if (sdk == HealthConnectClient.SDK_AVAILABLE) {
                val client = HealthConnectClient.getOrCreate(this@MainActivity)
                val granted = runCatching { client.permissionController.getGrantedPermissions() }.getOrDefault(emptySet())
                val n = granted.intersect(Perms.DATA).size
                lines += "Data permissions: $n of ${Perms.DATA.size} granted"
                val bgAvail = featureAvailable(client, HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND)
                lines += "Background read: " + when {
                    Perms.BACKGROUND in granted -> "granted (hourly sync on)"
                    !bgAvail -> "not supported here, syncs only when the app is opened"
                    else -> "not granted, syncs only when the app is opened"
                }
                lines += "History read (older than 30 days): " +
                    if (Perms.HISTORY in granted) "granted" else "not granted"
            }

            val shd = SamsungReader.status(this@MainActivity)
            lines += "Samsung Health: " + shd.message
            if (shd.state != SamsungReader.State.CONNECTED) {
                Status.prefs(this@MainActivity).getString("samsung_note", null)?.let { lines += "  last attempt: $it" }
            }
            lines += "Token: " + if (SyncRunner.hasToken()) "present" else "no token in this build"
            val p = Status.prefs(this@MainActivity)
            val t = p.getString("last_time", null)
            lines += ""
            lines += if (t == null) "Last sync: never" else
                "Last sync: $t (${p.getString("last_mode", "")})\nResult: ${p.getString("last_result", "")}"
            p.getString("bg_note", null)?.let { lines += it }
            p.getString("last_counts", null)?.let { c ->
                val pretty = runCatching {
                    val o = JSONObject(c)
                    o.keys().asSequence().joinToString("\n") { k -> "  $k: ${o.get(k)}" }
                }.getOrDefault(c)
                lines += "Server counts:\n$pretty"
            }
            status.text = lines.joinToString("\n")
        }
    }
}
