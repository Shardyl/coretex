package uk.coretex.healthbridge

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/** 7-day sync every 15 minutes. Only reads when the background-read permission is granted. */
class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val ctx = applicationContext
        if (HealthConnectClient.getSdkStatus(ctx) != HealthConnectClient.SDK_AVAILABLE) return Result.success()
        val granted = HealthConnectClient.getOrCreate(ctx).permissionController.getGrantedPermissions()
        if (Perms.BACKGROUND !in granted) {
            Status.note(ctx, "bg_note", "Background sync skipped: background read not granted")
            return Result.success()
        }
        Status.note(ctx, "bg_note", null)
        val outcome = SyncRunner.run(ctx, SyncMode.SYNC)
        return if (!outcome.ok && outcome.networkFailure) Result.retry() else Result.success()
    }

    companion object {
        // Name kept from the hourly era so UPDATE replaces the existing periodic work in place.
        private const val NAME = "health-bridge-hourly"

        fun schedule(ctx: Context) {
            val req = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(NAME, ExistingPeriodicWorkPolicy.UPDATE, req)
        }
    }
}
