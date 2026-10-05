package uk.coretex.healthbridge

import android.app.Activity
import android.content.Context
import com.samsung.android.sdk.health.data.HealthDataService
import com.samsung.android.sdk.health.data.HealthDataStore
import com.samsung.android.sdk.health.data.data.HealthDataPoint
import com.samsung.android.sdk.health.data.data.entries.ExerciseSession
import com.samsung.android.sdk.health.data.error.ErrorCode
import com.samsung.android.sdk.health.data.error.HealthDataException
import com.samsung.android.sdk.health.data.error.ResolvablePlatformException
import com.samsung.android.sdk.health.data.permission.AccessType
import com.samsung.android.sdk.health.data.permission.Permission
import com.samsung.android.sdk.health.data.request.DataType
import com.samsung.android.sdk.health.data.request.DataTypes
import com.samsung.android.sdk.health.data.request.InstantTimeFilter
import com.samsung.android.sdk.health.data.request.LocalTimeFilter
import com.samsung.android.sdk.health.data.request.LocalTimeGroup
import com.samsung.android.sdk.health.data.request.LocalTimeGroupUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * Reads workouts (exercise sessions + their heart rate) straight from Samsung Health via the
 * Samsung Health Data SDK, because Samsung Health does not share sessions or heart rate with
 * Health Connect. READ only. Every number comes from Samsung Health; missing data is JSON null.
 */
object SamsungReader {
    const val SOURCE = "com.sec.android.app.shealth"
    private val EXERCISE_READ: Permission = Permission.of(DataTypes.EXERCISE, AccessType.READ)
    private val HR_READ: Permission = Permission.of(DataTypes.HEART_RATE, AccessType.READ)
    private val STEPS_READ: Permission = Permission.of(DataTypes.STEPS, AccessType.READ)
    val PERMS: Set<Permission> = setOf(EXERCISE_READ, HR_READ, STEPS_READ)

    private val isoFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxxx")

    enum class State { CONNECTED, PERMISSION_NEEDED, NOT_AVAILABLE, ERROR }

    data class Status(val state: State, val message: String, val granted: Set<Permission> = emptySet())

    class Read(val sessions: JSONArray, val error: String?)

    /** Samsung Health daily step totals per LOCAL day; [permissionNeeded] = steps not granted. */
    class StepsRead(val byDay: Map<LocalDate, Long>, val error: String?, val permissionNeeded: Boolean = false)

    private fun store(ctx: Context): HealthDataStore = HealthDataService.getStore(ctx.applicationContext)

    /** Human-readable meaning of an SDK error code. */
    fun describe(e: Throwable): String {
        val code = (e as? HealthDataException)?.errorCode
        val what = when (code) {
            ErrorCode.ERR_PLATFORM_NOT_INSTALLED -> "Samsung Health not installed"
            ErrorCode.ERR_OLD_VERSION_PLATFORM -> "Samsung Health is outdated, update it"
            ErrorCode.ERR_PLATFORM_DISABLED -> "Samsung Health is disabled"
            ErrorCode.ERR_PLATFORM_NOT_INITIALIZED -> "open Samsung Health once and finish its setup"
            ErrorCode.ERR_NO_USER_PERMISSION -> "permission needed"
            ErrorCode.ERR_INVALID_CALLER, ErrorCode.ERR_ACCESS_CONTROL,
            ErrorCode.ERR_INVALID_PLATFORM_SIGNATURE -> "developer mode off? (Samsung Health refused this app)"
            ErrorCode.ERR_CHILD_ACCOUNT_ACCESS -> "not allowed on a child account"
            ErrorCode.ERR_CONNECTION_FAIL, ErrorCode.ERR_CONNECTION_TIMEOUT,
            ErrorCode.ERR_PLATFORM_DISCONNECTED, ErrorCode.ERR_INTERRUPTED -> "could not connect to Samsung Health"
            null -> e.javaClass.simpleName
            else -> "error"
        }
        val msg = (e as? HealthDataException)?.errorMessage ?: e.message
        val codeTxt = code?.let { " [$it]" }.orEmpty()
        return if (msg.isNullOrBlank()) "$what$codeTxt" else "$what$codeTxt: ${msg.take(160)}"
    }

    private fun stateOf(e: Throwable): State = when ((e as? HealthDataException)?.errorCode) {
        ErrorCode.ERR_PLATFORM_NOT_INSTALLED, ErrorCode.ERR_OLD_VERSION_PLATFORM,
        ErrorCode.ERR_PLATFORM_DISABLED, ErrorCode.ERR_PLATFORM_NOT_INITIALIZED -> State.NOT_AVAILABLE
        ErrorCode.ERR_NO_USER_PERMISSION -> State.PERMISSION_NEEDED
        else -> State.ERROR
    }

    suspend fun status(ctx: Context): Status = try {
        val granted = withContext(Dispatchers.IO) { store(ctx).getGrantedPermissions(PERMS) }
        when {
            EXERCISE_READ !in granted -> Status(State.PERMISSION_NEEDED, "permission needed", granted)
            STEPS_READ !in granted -> Status(State.PERMISSION_NEEDED, "steps permission needed", granted)
            else -> {
                val hr = if (HR_READ in granted) "" else " (heart rate permission not granted)"
                Status(State.CONNECTED, "connected$hr", granted)
            }
        }
    } catch (e: Throwable) {
        Status(stateOf(e), describe(e))
    }

    /** Opens Samsung Health's permission screen, or its own fix-up flow when it is not ready. */
    suspend fun connect(activity: Activity): String = try {
        val granted = store(activity).requestPermissions(PERMS, activity)
        val missing = buildList {
            if (EXERCISE_READ !in granted) add("workouts")
            if (STEPS_READ !in granted) add("steps")
            if (HR_READ !in granted) add("heart rate")
        }
        when {
            missing.isEmpty() -> "Samsung Health connected"
            missing.size == 3 -> "Samsung Health permission was not granted"
            else -> "Samsung Health connected (not granted: ${missing.joinToString(", ")})"
        }
    } catch (e: ResolvablePlatformException) {
        if (e.hasResolution) {
            runCatching { e.resolve(activity) }
            "Samsung Health needs attention: ${describe(e)}"
        } else "Samsung Health: ${describe(e)}"
    } catch (e: Throwable) {
        "Samsung Health: ${describe(e)}"
    }

    /**
     * Exercise sessions that START inside [from, toInclusive] (local days). Never throws:
     * any SDK failure comes back as [Read.error] so the Health Connect sync still goes out.
     */
    suspend fun read(ctx: Context, from: LocalDate, toInclusive: LocalDate): Read {
        val zone = ZoneId.systemDefault()
        val startI = from.atStartOfDay(zone).toInstant()
        val endI = toInclusive.plusDays(1).atStartOfDay(zone).toInstant()
        val out = JSONArray()
        return try {
            withContext(Dispatchers.IO) {
                val st = store(ctx)
                val granted = st.getGrantedPermissions(PERMS)
                if (EXERCISE_READ !in granted) return@withContext Read(out, null)
                val hrGranted = HR_READ in granted

                val records = readAll(st, DataTypes.EXERCISE.readDataRequestBuilder,
                    startI.minus(Duration.ofDays(1)), endI.plus(Duration.ofDays(1)))
                for (dp in records) {
                    val uid = dp.uid ?: continue
                    val sessions: List<ExerciseSession> =
                        runCatching { dp.getValue(DataType.ExerciseType.SESSIONS) }.getOrNull().orEmpty()
                    val recordTitle = runCatching { dp.getValue(DataType.ExerciseType.CUSTOM_TITLE) }.getOrNull()
                    val recordType = runCatching { dp.getValue(DataType.ExerciseType.EXERCISE_TYPE) }.getOrNull()
                    for ((idx, s) in sessions.withIndex()) {
                        val sStart = s.startTime ?: continue
                        if (sStart.isBefore(startI) || !sStart.isBefore(endI)) continue
                        val sEnd = s.endTime ?: sStart
                        val hr = sessionHr(st, s, hrGranted, sStart, sEnd)
                        val steps = if (s.countType == DataType.ExerciseType.CountType.STRIDE) s.count?.toDouble() else null
                        out.put(JSONObject().apply {
                            put("id", if (sessions.size > 1) "shd:$uid:$idx" else "shd:$uid")
                            put("start", iso(sStart, zone))
                            put("end", iso(sEnd, zone))
                            put("type", JSONObject.NULL) // the SDK exposes names only, no numeric code
                            putN("type_name", (s.exerciseType ?: recordType)?.name)
                            putN("title", s.customTitle?.takeIf { it.isNotBlank() } ?: recordTitle?.takeIf { it.isNotBlank() })
                            putN("kcal", s.calories.toDouble())
                            putN("avg_hr", hr?.first)
                            putN("max_hr", hr?.second)
                            putN("min_hr", hr?.third)
                            putN("distance_m", s.distance?.toDouble())
                            putN("steps", steps)
                            put("source", SOURCE)
                        })
                    }
                }
                Read(out, null)
            }
        } catch (e: Throwable) {
            Read(out, describe(e))
        }
    }

    /**
     * Samsung Health's own daily step totals for each LOCAL day in [from, toInclusive], using the
     * SDK aggregate `DataType.StepsType.TOTAL` grouped by the SDK per local day
     * (LocalTimeGroup DAILY x1). This is the number the Samsung Health app shows (it includes
     * steps taken inside workouts, which Samsung does not share with Health Connect).
     * Days Samsung returns no value (or 0) for are left out. Never throws.
     */
    suspend fun readDailySteps(ctx: Context, from: LocalDate, toInclusive: LocalDate): StepsRead {
        val out = HashMap<LocalDate, Long>()
        return try {
            withContext(Dispatchers.IO) {
                val st = store(ctx)
                if (STEPS_READ !in st.getGrantedPermissions(setOf(STEPS_READ)))
                    return@withContext StepsRead(out, null, permissionNeeded = true)
                val filter = LocalTimeFilter.of(from.atStartOfDay(), toInclusive.plusDays(1).atStartOfDay())
                val group = LocalTimeGroup.of(LocalTimeGroupUnit.DAILY, 1)
                var token: String? = null
                var guard = 0
                do {
                    val b = DataType.StepsType.TOTAL.requestBuilder.setLocalTimeFilterWithGroup(filter, group)
                    if (token != null) b.setPageToken(token)
                    val resp = st.aggregateData(b.build())
                    for (a in resp.dataList) {
                        val v = a.value ?: continue
                        if (v <= 0L) continue
                        val day = (runCatching { a.getStartLocalDateTime() }.getOrNull()?.toLocalDate()
                            ?: a.startTime?.atZone(ZoneId.systemDefault())?.toLocalDate()) ?: continue
                        if (day.isBefore(from) || day.isAfter(toInclusive)) continue
                        out[day] = v
                    }
                    val next = resp.pageToken
                    token = if (next.isNullOrEmpty() || next == token) null else next
                } while (token != null && ++guard < 50)
                StepsRead(out, null)
            }
        } catch (e: Throwable) {
            StepsRead(emptyMap(), describe(e))
        }
    }

    /** Daytime resting heart rate per LOCAL day; [error] = the SDK read failed. */
    class RestingRead(val byDay: Map<LocalDate, Double>, val error: String?)

    private val DAY_FROM: java.time.LocalTime = java.time.LocalTime.of(6, 0)
    private val DAY_TO: java.time.LocalTime = java.time.LocalTime.of(20, 0)
    private val AFTER_EXERCISE: Duration = Duration.ofMinutes(10)
    private val REST_WINDOW: Duration = Duration.ofMinutes(10)
    private const val REST_MIN_SAMPLES = 5

    /**
     * Daytime resting HR for each LOCAL day in [from, toInclusive] (the watch is worn ~06:00-18:00,
     * not at night). Samsung HEART_RATE samples between 06:00 and 20:00 local, excluding every
     * Samsung exercise session plus 10 minutes after it ends; result = the lowest mean over any
     * 10-minute window holding at least 5 samples. Days without such a window are left out.
     * Needs HEART_RATE and EXERCISE read (without EXERCISE workouts could not be excluded, so
     * nothing is computed). Never throws.
     */
    suspend fun readDaytimeRestingHr(ctx: Context, from: LocalDate, toInclusive: LocalDate): RestingRead {
        val zone = ZoneId.systemDefault()
        val out = HashMap<LocalDate, Double>()
        return try {
            withContext(Dispatchers.IO) {
                val st = store(ctx)
                val granted = st.getGrantedPermissions(PERMS)
                if (HR_READ !in granted || EXERCISE_READ !in granted) return@withContext RestingRead(out, null)

                // Exercise windows (+10 min) over the whole range, read once.
                val rangeStart = from.atStartOfDay(zone).toInstant()
                val rangeEnd = toInclusive.plusDays(1).atStartOfDay(zone).toInstant()
                val excluded = ArrayList<Pair<Instant, Instant>>()
                for (dp in readAll(st, DataTypes.EXERCISE.readDataRequestBuilder,
                    rangeStart.minus(Duration.ofDays(1)), rangeEnd.plus(Duration.ofDays(1)))) {
                    val sessions: List<ExerciseSession> =
                        runCatching { dp.getValue(DataType.ExerciseType.SESSIONS) }.getOrNull().orEmpty()
                    if (sessions.isEmpty()) {
                        val s = dp.startTime ?: continue
                        excluded += s to (dp.endTime ?: s).plus(AFTER_EXERCISE)
                    }
                    for (s in sessions) {
                        val a = s.startTime ?: continue
                        excluded += a to (s.endTime ?: a).plus(AFTER_EXERCISE)
                    }
                }
                fun isExcluded(t: Instant) = excluded.any { (a, b) -> !t.isBefore(a) && t.isBefore(b) }

                var d = from
                while (!d.isAfter(toInclusive)) {
                    val dStart = d.atTime(DAY_FROM).atZone(zone).toInstant()
                    val dEnd = d.atTime(DAY_TO).atZone(zone).toInstant()
                    val samples = ArrayList<Pair<Instant, Double>>()
                    fun take(t: Instant?, bpm: Float?) {
                        if (t == null || bpm == null || bpm <= 0f) return
                        if (t.isBefore(dStart) || !t.isBefore(dEnd) || isExcluded(t)) return
                        samples += t to bpm.toDouble()
                    }
                    for (p in readAll(st, DataTypes.HEART_RATE.readDataRequestBuilder, dStart, dEnd)) {
                        val series = runCatching { p.getValue(DataType.HeartRateType.SERIES_DATA) }.getOrNull().orEmpty()
                        if (series.isNotEmpty()) {
                            for (e in series) take(e.startTime, e.heartRate)
                        } else {
                            take(p.startTime, runCatching { p.getValue(DataType.HeartRateType.HEART_RATE) }.getOrNull())
                        }
                    }
                    samples.sortBy { it.first }
                    lowestRollingAvg(samples, REST_WINDOW, REST_MIN_SAMPLES)?.let { out[d] = it }
                    d = d.plusDays(1)
                }
                RestingRead(out, null)
            }
        } catch (e: Throwable) {
            RestingRead(emptyMap(), describe(e))
        }
    }

    /**
     * (mean, max, min) heart rate for a session. Order: the session's own stats, else the
     * session's exercise log, else Samsung heart-rate samples inside the session window.
     */
    private suspend fun sessionHr(
        st: HealthDataStore, s: ExerciseSession, hrGranted: Boolean, start: Instant, end: Instant,
    ): Triple<Double?, Double?, Double?>? {
        val mean = s.meanHeartRate?.toDouble()?.takeIf { it > 0 }
        val max = s.maxHeartRate?.toDouble()?.takeIf { it > 0 }
        val min = s.minHeartRate?.toDouble()?.takeIf { it > 0 }
        if (mean != null || max != null || min != null) return Triple(mean, max, min)

        val logHr = s.log.orEmpty().mapNotNull { it.heartRate?.toDouble()?.takeIf { v -> v > 0 } }
        if (logHr.isNotEmpty()) return Triple(logHr.average(), logHr.max(), logHr.min())

        if (!hrGranted || !end.isAfter(start)) return null
        val samples = ArrayList<Double>()
        val points = readAll(st, DataTypes.HEART_RATE.readDataRequestBuilder, start, end)
        for (p in points) {
            val series = runCatching { p.getValue(DataType.HeartRateType.SERIES_DATA) }.getOrNull().orEmpty()
            if (series.isNotEmpty()) {
                for (e in series) {
                    val t = e.startTime ?: continue
                    if (t.isBefore(start) || t.isAfter(end)) continue
                    e.heartRate.toDouble().takeIf { it > 0 }?.let { samples += it }
                }
            } else {
                val t = p.startTime ?: continue
                if (t.isBefore(start) || t.isAfter(end)) continue
                runCatching { p.getValue(DataType.HeartRateType.HEART_RATE) }.getOrNull()
                    ?.toDouble()?.takeIf { it > 0 }?.let { samples += it }
            }
        }
        return if (samples.isEmpty()) null else Triple(samples.average(), samples.max(), samples.min())
    }

    private suspend fun readAll(
        st: HealthDataStore,
        builder: com.samsung.android.sdk.health.data.request.ReadDataRequest.DualTimeBuilder<HealthDataPoint>,
        start: Instant, end: Instant,
    ): List<HealthDataPoint> {
        val out = ArrayList<HealthDataPoint>()
        var token: String? = null
        var guard = 0
        do {
            val b = builder.setInstantTimeFilter(InstantTimeFilter.of(start, end))
            if (token != null) b.setPageToken(token)
            val resp = st.readData(b.build())
            out += resp.dataList
            val next = resp.pageToken
            token = if (next.isNullOrEmpty() || next == token) null else next
        } while (token != null && ++guard < 200)
        return out
    }

    private fun iso(t: Instant, zone: ZoneId): String =
        OffsetDateTime.ofInstant(t, zone.rules.getOffset(t)).format(isoFmt)
}
