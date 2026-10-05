package uk.coretex.healthbridge

import android.content.Context
import android.os.Build
import androidx.health.connect.client.HealthConnectClient
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import java.time.LocalDate

enum class SyncMode(val label: String) { SYNC("7-day sync"), BACKFILL("backfill") }

data class SyncOutcome(val ok: Boolean, val message: String, val networkFailure: Boolean = false)

object SyncRunner {
    private val lock = Mutex()
    val BACKFILL_FROM: LocalDate = LocalDate.of(2026, 1, 1)
    private const val SYNC_DAYS = 7L

    fun hasToken() = BuildConfig.BRIDGE_TOKEN.isNotBlank()

    suspend fun run(ctx: Context, mode: SyncMode, progress: (String) -> Unit = {}): SyncOutcome =
        lock.withLock { runLocked(ctx.applicationContext, mode, progress) }

    private suspend fun runLocked(ctx: Context, mode: SyncMode, progress: (String) -> Unit): SyncOutcome {
        fun finish(o: SyncOutcome, counts: JSONObject? = null): SyncOutcome {
            Status.record(ctx, mode.label, o.message, counts?.toString())
            return o
        }
        if (!hasToken()) return finish(SyncOutcome(false, "No token in this build, nothing sent"))
        if (HealthConnectClient.getSdkStatus(ctx) != HealthConnectClient.SDK_AVAILABLE)
            return finish(SyncOutcome(false, "Health Connect not available"))

        val client = HealthConnectClient.getOrCreate(ctx)
        val granted = client.permissionController.getGrantedPermissions()
        if (granted.intersect(Perms.DATA).isEmpty())
            return finish(SyncOutcome(false, "No Health Connect permissions granted"))

        val today = LocalDate.now()
        val chunks: List<Pair<LocalDate, LocalDate>> = when (mode) {
            SyncMode.SYNC -> listOf(today.minusDays(SYNC_DAYS - 1) to today)
            SyncMode.BACKFILL -> buildList {
                var start = BACKFILL_FROM
                while (!start.isAfter(today)) {
                    val end = minOf(start.withDayOfMonth(start.lengthOfMonth()), today)
                    add(start to end)
                    start = end.plusDays(1)
                }
            }
        }

        val reader = HealthReader(client, granted)
        var lastCounts: JSONObject? = null
        var sentDays = 0; var sentSessions = 0; var sentSamsung = 0; var sentWeights = 0
        var samsungError: String? = null
        var stepsError: String? = null
        var stepsPermNeeded = false
        var samsungStepDays = 0
        var sleepDetailDays = 0
        var restingDays = 0
        var restError: String? = null
        val respMissing = Perms.RESPIRATORY !in granted
        for ((i, c) in chunks.withIndex()) {
            progress("Reading ${c.first} to ${c.second} (${i + 1}/${chunks.size})")
            val chunk = try {
                reader.read(c.first, c.second)
            } catch (e: SecurityException) {
                return finish(SyncOutcome(false, "Health Connect refused the read (${e.message ?: "permission"})"))
            } catch (e: Exception) {
                return finish(SyncOutcome(false, "Read failed at ${c.first}: ${e.javaClass.simpleName} ${e.message.orEmpty()}".trim()))
            }
            // Samsung Health workouts (not shared with Health Connect). Failure never blocks the HC sync.
            val shd = SamsungReader.read(ctx, c.first, c.second)
            if (shd.error != null && samsungError == null) samsungError = shd.error
            for (k in 0 until shd.sessions.length()) chunk.sessions.put(shd.sessions.get(k))
            // Daily steps: Samsung Health's own total overrides Health Connect's (HC gets only partial,
            // late steps from Samsung). Days Samsung has no value for keep the HC number. Failure = HC steps.
            val steps = SamsungReader.readDailySteps(ctx, c.first, c.second)
            if (steps.error != null && stepsError == null) stepsError = steps.error
            if (steps.permissionNeeded) stepsPermNeeded = true
            var chunkStepDays = 0
            for (k in 0 until chunk.days.length()) {
                val day = chunk.days.getJSONObject(k)
                val v = runCatching { LocalDate.parse(day.getString("day")) }.getOrNull()?.let { steps.byDay[it] } ?: continue
                day.put("steps", v)
                chunkStepDays++
            }
            // Daytime resting HR from Samsung Health (watch worn by day only). Failure = nulls.
            val rest = SamsungReader.readDaytimeRestingHr(ctx, c.first, c.second)
            if (rest.error != null && restError == null) restError = rest.error
            var chunkRestDays = 0
            var chunkSleepDays = 0
            for (k in 0 until chunk.days.length()) {
                val day = chunk.days.getJSONObject(k)
                if (!day.isNull("sleep_start")) chunkSleepDays++
                val v = runCatching { LocalDate.parse(day.getString("day")) }.getOrNull()?.let { rest.byDay[it] } ?: continue
                day.put("day_resting_hr", v)
                chunkRestDays++
            }
            val body = JSONObject().apply {
                put("device", Build.MODEL)
                put("app_version", BuildConfig.VERSION_NAME)
                put("days", chunk.days)
                put("sessions", chunk.sessions)
                put("weights", chunk.weights)
            }
            progress("Sending ${c.first} to ${c.second} (${i + 1}/${chunks.size})")
            when (val r = Uploader.post(body.toString())) {
                is UploadResult.Ok -> {
                    lastCounts = r.counts
                    sentDays += chunk.days.length(); sentSessions += chunk.sessions.length(); sentWeights += chunk.weights.length()
                    sentSamsung += shd.sessions.length()
                    samsungStepDays += chunkStepDays
                    sleepDetailDays += chunkSleepDays
                    restingDays += chunkRestDays
                }
                UploadResult.Unauthorized -> return finish(SyncOutcome(false, "Server rejected the token (401)"))
                is UploadResult.Failed -> return finish(
                    SyncOutcome(false, "Upload failed at ${c.first}: ${r.message}", networkFailure = r.network)
                )
            }
        }
        progress("")
        return finish(
            SyncOutcome(
                true,
                "OK: sent $sentDays days (steps from Samsung for $samsungStepDays days), " +
                    "$sentSessions sessions ($sentSamsung from Samsung), $sentWeights weights, " +
                    "sleep detail for $sleepDetailDays days, daytime resting HR for $restingDays days" +
                    (if (respMissing) "\nRespiratory rate not granted: tap Grant permissions" else "") +
                    (restError?.let { "\nSamsung daytime resting HR failed: $it" } ?: "") +
                    (stepsError?.let { "\nSamsung steps failed, Health Connect steps used: $it" } ?: "") +
                    (if (stepsPermNeeded && stepsError == null)
                        "\nSamsung Health: steps permission needed, Health Connect steps used" else "") +
                    (samsungError?.let { "\nSamsung Health: $it" } ?: ""),
            ),
            lastCounts,
        )
    }
}
