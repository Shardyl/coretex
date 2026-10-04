package uk.coretex.healthbridge

import android.content.Context
import android.content.SharedPreferences

/** Last-sync status shared between the UI and the background worker. */
object Status {
    const val PREFS = "bridge_status"
    fun prefs(ctx: Context): SharedPreferences = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun record(ctx: Context, mode: String, result: String, counts: String?) {
        prefs(ctx).edit()
            .putString("last_time", java.time.ZonedDateTime.now().withNano(0).toString())
            .putString("last_mode", mode)
            .putString("last_result", result)
            .apply {
                if (counts != null) putString("last_counts", counts)
            }
            .apply()
    }

    fun note(ctx: Context, key: String, value: String?) {
        prefs(ctx).edit().putString(key, value).apply()
    }
}
