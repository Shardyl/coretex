package uk.coretex.healthbridge

import android.app.Activity
import android.content.Context
import android.os.Bundle
import android.widget.Toast
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.CompletableDeferred

/**
 * cortexbridge://sync — fired by the Fitness PWA. No UI: runs the normal 7-day sync (same path as
 * "Sync now", SyncRunner + SyncMode.SYNC, so token / Health Connect / permission checks and their
 * messages are identical), toasts the result and finishes so Android returns to the calling app.
 * The activity waits at most ~20 s; the sync itself runs in an app-level scope and keeps going if
 * it takes longer, toasting its result when done.
 */
class SyncNowActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (savedInstanceState != null) { finish(); return }
        val app = applicationContext
        toast(app, "Syncing to Cortex…")
        val done = CompletableDeferred<SyncOutcome>()
        appScope.launch {
            val outcome = try {
                SyncRunner.run(app, SyncMode.SYNC)
            } catch (e: Throwable) {
                SyncOutcome(false, "Sync failed: ${e.javaClass.simpleName} ${e.message.orEmpty()}".trim())
            }
            done.complete(outcome)
            withContext(Dispatchers.Main) { toast(app, summary(outcome)) }
        }
        appScope.launch(Dispatchers.Main) {
            withTimeoutOrNull(MAX_WAIT_MS) { done.await() }
            finish()
        }
    }

    companion object {
        private const val MAX_WAIT_MS = 20_000L
        private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

        private fun summary(o: SyncOutcome): String =
            if (o.ok) "Synced" else o.message.lineSequence().firstOrNull().orEmpty().ifBlank { "Sync failed" }

        private fun toast(ctx: Context, msg: String) =
            Toast.makeText(ctx, msg, if (msg.length > 30) Toast.LENGTH_LONG else Toast.LENGTH_SHORT).show()
    }
}
