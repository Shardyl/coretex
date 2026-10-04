package uk.coretex.healthbridge

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

sealed class UploadResult {
    data class Ok(val counts: JSONObject?) : UploadResult()
    object Unauthorized : UploadResult()
    data class Failed(val message: String, val network: Boolean) : UploadResult()
}

object Uploader {
    private const val ATTEMPTS = 4

    /** POST with exponential backoff (2s, 4s, 8s) on network errors, 429 and 5xx. 401 is final. */
    suspend fun post(body: String): UploadResult {
        var last: UploadResult = UploadResult.Failed("not attempted", true)
        for (attempt in 0 until ATTEMPTS) {
            if (attempt > 0) delay(2000L shl (attempt - 1))
            last = withContext(Dispatchers.IO) { once(body) }
            val retry = when (val r = last) {
                is UploadResult.Ok, UploadResult.Unauthorized -> false
                is UploadResult.Failed -> r.network
            }
            if (!retry) return last
        }
        return last
    }

    private fun once(body: String): UploadResult {
        val conn = (URL(BuildConfig.API_URL).openConnection() as HttpURLConnection)
        return try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 15_000
            conn.readTimeout = 60_000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            conn.setRequestProperty("Accept", "application/json")
            conn.setRequestProperty("Authorization", "Bearer ${BuildConfig.BRIDGE_TOKEN}")
            conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val text = (if (code in 200..299) conn.inputStream else conn.errorStream)
                ?.bufferedReader()?.use { it.readText() }.orEmpty()
            when {
                code == 401 || code == 403 -> UploadResult.Unauthorized
                code in 200..299 -> {
                    val json = runCatching { JSONObject(text) }.getOrNull()
                    if (json?.optBoolean("ok") == true) UploadResult.Ok(json.optJSONObject("counts"))
                    else UploadResult.Failed("server replied without ok: ${text.take(200)}", false)
                }
                code == 429 || code >= 500 -> UploadResult.Failed("HTTP $code", true)
                else -> UploadResult.Failed("HTTP $code ${text.take(200)}", false)
            }
        } catch (e: IOException) {
            UploadResult.Failed("network: ${e.javaClass.simpleName} ${e.message.orEmpty()}".trim(), true)
        } finally {
            conn.disconnect()
        }
    }
}
