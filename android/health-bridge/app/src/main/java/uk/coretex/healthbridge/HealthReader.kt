package uk.coretex.healthbridge

import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.aggregate.AggregateMetric
import androidx.health.connect.client.aggregate.AggregationResult
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.BodyFatRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.FloorsClimbedRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RespiratoryRateRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import org.json.JSONArray
import org.json.JSONObject
import java.lang.reflect.Modifier
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.Period
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import kotlin.reflect.KClass

/**
 * Reads one window of LOCAL calendar days from Health Connect and turns it into the
 * days / sessions / weights arrays of the bridge payload. Every number comes from
 * Health Connect; missing data is JSON null, never a default.
 */
class HealthReader(
    private val client: HealthConnectClient,
    private val granted: Set<String>,
) {
    private val zone: ZoneId = ZoneId.systemDefault()
    private val isoFmt: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssxxx")

    class Chunk(val days: JSONArray, val sessions: JSONArray, val weights: JSONArray)

    private fun has(type: KClass<out Record>) = HealthPermission.getReadPermission(type) in granted

    suspend fun read(from: LocalDate, toInclusive: LocalDate): Chunk {
        val startLdt = from.atStartOfDay()
        val endLdt = toInclusive.plusDays(1).atStartOfDay()
        val startI = startLdt.atZone(zone).toInstant()
        val endI = endLdt.atZone(zone).toInstant()
        return Chunk(
            days = readDays(from, toInclusive, startLdt, endLdt, startI, endI),
            sessions = readSessions(startI, endI),
            weights = readWeights(startI, endI),
        )
    }

    // ---------- daily totals ----------

    private suspend fun readDays(
        from: LocalDate, toInclusive: LocalDate,
        startLdt: java.time.LocalDateTime, endLdt: java.time.LocalDateTime,
        startI: Instant, endI: Instant,
    ): JSONArray {
        val metrics = mutableSetOf<AggregateMetric<*>>()
        if (has(StepsRecord::class)) metrics += StepsRecord.COUNT_TOTAL
        if (has(TotalCaloriesBurnedRecord::class)) metrics += TotalCaloriesBurnedRecord.ENERGY_TOTAL
        if (has(ActiveCaloriesBurnedRecord::class)) metrics += ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL
        if (has(DistanceRecord::class)) metrics += DistanceRecord.DISTANCE_TOTAL
        if (has(FloorsClimbedRecord::class)) metrics += FloorsClimbedRecord.FLOORS_CLIMBED_TOTAL
        if (has(RestingHeartRateRecord::class)) metrics += RestingHeartRateRecord.BPM_AVG

        val byDay = HashMap<LocalDate, AggregationResult>()
        if (metrics.isNotEmpty()) {
            val groups = client.aggregateGroupByPeriod(
                AggregateGroupByPeriodRequest(
                    metrics = metrics,
                    timeRangeFilter = TimeRangeFilter.between(startLdt, endLdt),
                    timeRangeSlicer = Period.ofDays(1),
                )
            )
            for (g in groups) byDay[g.startTime.toLocalDate()] = g.result
        }

        // HRV: average of RMSSD samples per local day.
        val hrv = HashMap<LocalDate, MutableList<Double>>()
        if (has(HeartRateVariabilityRmssdRecord::class)) {
            for (r in readAll(HeartRateVariabilityRmssdRecord::class, TimeRangeFilter.between(startI, endI))) {
                hrv.getOrPut(r.time.atZone(zone).toLocalDate()) { mutableListOf() } += r.heartRateVariabilityMillis
            }
        }

        // Sleep: total minutes of sessions that ENDED on the day.
        val sleep = HashMap<LocalDate, Double>()
        // Main (longest) sleep session that ENDED on the day, for the sleep detail fields.
        val mainSleep = HashMap<LocalDate, SleepSessionRecord>()
        if (has(SleepSessionRecord::class)) {
            val readFrom = startI.minus(Duration.ofDays(2))
            for (r in readAll(SleepSessionRecord::class, TimeRangeFilter.between(readFrom, endI))) {
                val endDay = r.endTime.atZone(zone).toLocalDate()
                if (endDay < from || endDay > toInclusive) continue
                val mins = Duration.between(r.startTime, r.endTime).seconds / 60.0
                sleep[endDay] = (sleep[endDay] ?: 0.0) + mins
                val cur = mainSleep[endDay]
                if (cur == null ||
                    Duration.between(r.startTime, r.endTime) > Duration.between(cur.startTime, cur.endTime)
                ) mainSleep[endDay] = r
            }
        }
        val sleepDetail = HashMap<LocalDate, SleepDetail>()
        for ((day, s) in mainSleep) sleepDetail[day] = sleepDetail(s)

        val out = JSONArray()
        var d = from
        while (!d.isAfter(toInclusive)) {
            val a = byDay[d]
            out.put(JSONObject().apply {
                put("day", d.toString())
                putN("steps", a?.get(StepsRecord.COUNT_TOTAL))
                // With no real records, Health Connect still returns a TOTAL made of its default
                // basal estimate (1,565 kcal on this phone, every day). That is not the owner's data,
                // so a total is only sent when some app actually contributed records that day.
                val real = a != null && a.dataOrigins.isNotEmpty()
                putN("total_kcal", if (real) a?.get(TotalCaloriesBurnedRecord.ENERGY_TOTAL)?.inKilocalories else null)
                putN("active_kcal", a?.get(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL)?.inKilocalories)
                putN("distance_m", a?.get(DistanceRecord.DISTANCE_TOTAL)?.inMeters)
                putN("floors", a?.get(FloorsClimbedRecord.FLOORS_CLIMBED_TOTAL))
                putN("resting_hr", a?.get(RestingHeartRateRecord.BPM_AVG)?.toDouble())
                putN("hrv_ms", hrv[d]?.takeIf { it.isNotEmpty() }?.average())
                putN("sleep_min", sleep[d])
                val sd = sleepDetail[d]
                putN("sleep_deep_min", sd?.deep)
                putN("sleep_rem_min", sd?.rem)
                putN("sleep_light_min", sd?.light)
                putN("sleep_awake_min", sd?.awake)
                putN("sleep_start", sd?.start)
                putN("sleep_end", sd?.end)
                putN("sleep_hr_avg", sd?.hrAvg)
                putN("sleep_hr_min", sd?.hrMin)
                putN("sleep_resp_avg", sd?.respAvg)
                putN("sleep_hrv_ms", sd?.hrvMs)
                putN("sleep_source", sd?.source)
                // Filled from Samsung Health by SyncRunner when the watch has enough daytime data.
                put("day_resting_hr", JSONObject.NULL)
                put("day_hr_samples", JSONObject.NULL)
                put("day_hr_gap_s", JSONObject.NULL)
            })
            d = d.plusDays(1)
        }
        return out
    }

    // ---------- sleep detail ----------

    private class SleepDetail(
        val deep: Double?, val rem: Double?, val light: Double?, val awake: Double?,
        val start: String, val end: String,
        val hrAvg: Double?, val hrMin: Double?, val respAvg: Double?, val hrvMs: Double?,
        val source: String,
    )

    /**
     * Stage minutes plus overnight heart rate, respiratory rate and HRV for one sleep session.
     * Vitals are any Health Connect source's records inside the session window; null when absent.
     */
    private suspend fun sleepDetail(s: SleepSessionRecord): SleepDetail {
        var deep: Double? = null; var rem: Double? = null; var light: Double? = null; var awake: Double? = null
        if (s.stages.isNotEmpty()) {
            var dp = 0.0; var rm = 0.0; var lt = 0.0; var aw = 0.0
            for (st in s.stages) {
                val m = Duration.between(st.startTime, st.endTime).seconds / 60.0
                if (m <= 0) continue
                when (st.stage) {
                    SleepSessionRecord.STAGE_TYPE_DEEP -> dp += m
                    SleepSessionRecord.STAGE_TYPE_REM -> rm += m
                    SleepSessionRecord.STAGE_TYPE_LIGHT -> lt += m
                    SleepSessionRecord.STAGE_TYPE_AWAKE, SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED,
                    SleepSessionRecord.STAGE_TYPE_OUT_OF_BED -> aw += m
                }
            }
            deep = dp; rem = rm; light = lt; awake = aw
        }
        val win = TimeRangeFilter.between(s.startTime, s.endTime)
        fun inWin(t: Instant) = !t.isBefore(s.startTime) && !t.isAfter(s.endTime)

        var hrAvg: Double? = null
        var hrMin: Double? = null
        if (has(HeartRateRecord::class)) {
            val samples = readAll(HeartRateRecord::class, win)
                .flatMap { it.samples }
                .filter { inWin(it.time) && it.beatsPerMinute > 0 }
                .map { it.time to it.beatsPerMinute.toDouble() }
                .sortedBy { it.first }
            if (samples.isNotEmpty()) {
                hrAvg = samples.map { it.second }.average()
                // Lowest 5-minute rolling average when samples are dense enough (3+ in a window),
                // else the lowest single sample.
                hrMin = lowestRollingAvg(samples, Duration.ofMinutes(5), 3) ?: samples.minOf { it.second }
            }
        }
        val resp = if (has(RespiratoryRateRecord::class))
            readAll(RespiratoryRateRecord::class, win).filter { inWin(it.time) && it.rate > 0 }.map { it.rate }
        else emptyList()
        val hrv = if (has(HeartRateVariabilityRmssdRecord::class))
            readAll(HeartRateVariabilityRmssdRecord::class, win)
                .filter { inWin(it.time) && it.heartRateVariabilityMillis > 0 }
                .map { it.heartRateVariabilityMillis }
        else emptyList()

        return SleepDetail(
            deep = deep, rem = rem, light = light, awake = awake,
            start = iso(s.startTime, s.startZoneOffset), end = iso(s.endTime, s.endZoneOffset),
            hrAvg = hrAvg, hrMin = hrMin,
            respAvg = resp.takeIf { it.isNotEmpty() }?.average(),
            hrvMs = hrv.takeIf { it.isNotEmpty() }?.average(),
            source = s.metadata.dataOrigin.packageName,
        )
    }

    // ---------- exercise sessions ----------

    private suspend fun readSessions(startI: Instant, endI: Instant): JSONArray {
        val out = JSONArray()
        if (!has(ExerciseSessionRecord::class)) return out
        // Read a little wider, then keep sessions that START inside the window so a
        // session crossing a chunk boundary is sent exactly once.
        val records = readAll(
            ExerciseSessionRecord::class,
            TimeRangeFilter.between(startI.minus(Duration.ofDays(1)), endI.plus(Duration.ofDays(1))),
        ).filter { !it.startTime.isBefore(startI) && it.startTime.isBefore(endI) }

        val metrics = mutableSetOf<AggregateMetric<*>>()
        if (has(HeartRateRecord::class)) {
            metrics += HeartRateRecord.BPM_AVG; metrics += HeartRateRecord.BPM_MAX; metrics += HeartRateRecord.BPM_MIN
        }
        if (has(TotalCaloriesBurnedRecord::class)) metrics += TotalCaloriesBurnedRecord.ENERGY_TOTAL
        if (has(ActiveCaloriesBurnedRecord::class)) metrics += ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL
        if (has(DistanceRecord::class)) metrics += DistanceRecord.DISTANCE_TOTAL
        if (has(StepsRecord::class)) metrics += StepsRecord.COUNT_TOTAL

        for (s in records) {
            val a: AggregationResult? = if (metrics.isEmpty()) null else client.aggregate(
                AggregateRequest(metrics, TimeRangeFilter.between(s.startTime, s.endTime))
            )
            val kcal = a?.get(TotalCaloriesBurnedRecord.ENERGY_TOTAL)?.inKilocalories
                ?: a?.get(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL)?.inKilocalories
            out.put(JSONObject().apply {
                put("id", s.metadata.id)
                put("start", iso(s.startTime, s.startZoneOffset))
                put("end", iso(s.endTime, s.endZoneOffset))
                put("type", s.exerciseType)
                put("type_name", exerciseTypeNames[s.exerciseType] ?: "UNKNOWN_${s.exerciseType}")
                putN("title", s.title)
                putN("kcal", kcal)
                putN("avg_hr", a?.get(HeartRateRecord.BPM_AVG)?.toDouble())
                putN("max_hr", a?.get(HeartRateRecord.BPM_MAX)?.toDouble())
                putN("min_hr", a?.get(HeartRateRecord.BPM_MIN)?.toDouble())
                putN("distance_m", a?.get(DistanceRecord.DISTANCE_TOTAL)?.inMeters)
                putN("steps", a?.get(StepsRecord.COUNT_TOTAL)?.toDouble())
                put("source", s.metadata.dataOrigin.packageName)
            })
        }
        return out
    }

    // ---------- weight + body fat ----------

    private suspend fun readWeights(startI: Instant, endI: Instant): JSONArray {
        val out = JSONArray()
        if (!has(WeightRecord::class)) return out
        val weights = readAll(WeightRecord::class, TimeRangeFilter.between(startI, endI))
        val fats = if (has(BodyFatRecord::class))
            readAll(BodyFatRecord::class, TimeRangeFilter.between(startI, endI)) else emptyList()
        for (w in weights) {
            val day = w.time.atZone(zone).toLocalDate()
            val fat = fats
                .filter { it.time.atZone(zone).toLocalDate() == day }
                .minByOrNull { Duration.between(it.time, w.time).abs() }
            out.put(JSONObject().apply {
                put("time", iso(w.time, w.zoneOffset))
                put("kg", w.weight.inKilograms)
                putN("body_fat_pct", fat?.percentage?.value)
                put("source", w.metadata.dataOrigin.packageName)
            })
        }
        return out
    }

    // ---------- helpers ----------

    private suspend fun <T : Record> readAll(type: KClass<T>, filter: TimeRangeFilter): List<T> {
        val out = ArrayList<T>()
        var token: String? = null
        do {
            val resp = client.readRecords(
                ReadRecordsRequest(recordType = type, timeRangeFilter = filter, pageSize = 1000, pageToken = token)
            )
            out += resp.records
            token = resp.pageToken
        } while (!token.isNullOrEmpty())
        return out
    }

    private fun iso(t: Instant, off: ZoneOffset?): String =
        OffsetDateTime.ofInstant(t, off ?: zone.rules.getOffset(t)).format(isoFmt)

    companion object {
        /** EXERCISE_TYPE_* constant name by value, read from the SDK so names stay exact. */
        val exerciseTypeNames: Map<Int, String> by lazy {
            ExerciseSessionRecord::class.java.fields
                .filter {
                    Modifier.isStatic(it.modifiers) && it.type == Int::class.javaPrimitiveType &&
                        it.name.startsWith("EXERCISE_TYPE_")
                }
                .associate { it.getInt(null) to it.name }
        }
    }
}

/**
 * Lowest mean over any window [t, t + width) that starts at a sample and holds at least
 * [minSamples] samples. [samples] must be sorted by time. Null when no window qualifies.
 */
internal fun lowestRollingAvg(samples: List<Pair<Instant, Double>>, width: Duration, minSamples: Int): Double? {
    var best: Double? = null
    var j = 0
    var sum = 0.0
    for (i in samples.indices) {
        if (j < i) { j = i; sum = 0.0 }
        val limit = samples[i].first.plus(width)
        while (j < samples.size && samples[j].first.isBefore(limit)) { sum += samples[j].second; j++ }
        val n = j - i
        if (n >= minSamples) { val avg = sum / n; if (best == null || avg < best) best = avg }
        if (j > i) sum -= samples[i].second
    }
    return best
}

/** Median of [values] (mean of the two middle values when even, rounded), null when empty. */
internal fun medianOf(values: List<Long>): Long? {
    if (values.isEmpty()) return null
    val v = values.sorted()
    val m = v.size / 2
    return if (v.size % 2 == 1) v[m] else Math.round((v[m - 1] + v[m]) / 2.0)
}

internal fun JSONObject.putN(key: String, value: Any?): JSONObject = put(key, value ?: JSONObject.NULL)
