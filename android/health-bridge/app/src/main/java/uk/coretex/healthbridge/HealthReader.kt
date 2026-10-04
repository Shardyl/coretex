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
        if (has(SleepSessionRecord::class)) {
            val readFrom = startI.minus(Duration.ofDays(2))
            for (r in readAll(SleepSessionRecord::class, TimeRangeFilter.between(readFrom, endI))) {
                val endDay = r.endTime.atZone(zone).toLocalDate()
                if (endDay < from || endDay > toInclusive) continue
                val mins = Duration.between(r.startTime, r.endTime).seconds / 60.0
                sleep[endDay] = (sleep[endDay] ?: 0.0) + mins
            }
        }

        val out = JSONArray()
        var d = from
        while (!d.isAfter(toInclusive)) {
            val a = byDay[d]
            out.put(JSONObject().apply {
                put("day", d.toString())
                putN("steps", a?.get(StepsRecord.COUNT_TOTAL))
                putN("total_kcal", a?.get(TotalCaloriesBurnedRecord.ENERGY_TOTAL)?.inKilocalories)
                putN("active_kcal", a?.get(ActiveCaloriesBurnedRecord.ACTIVE_CALORIES_TOTAL)?.inKilocalories)
                putN("distance_m", a?.get(DistanceRecord.DISTANCE_TOTAL)?.inMeters)
                putN("floors", a?.get(FloorsClimbedRecord.FLOORS_CLIMBED_TOTAL))
                putN("resting_hr", a?.get(RestingHeartRateRecord.BPM_AVG)?.toDouble())
                putN("hrv_ms", hrv[d]?.takeIf { it.isNotEmpty() }?.average())
                putN("sleep_min", sleep[d])
            })
            d = d.plusDays(1)
        }
        return out
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

internal fun JSONObject.putN(key: String, value: Any?): JSONObject = put(key, value ?: JSONObject.NULL)
