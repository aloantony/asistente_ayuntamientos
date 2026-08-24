/*
 * Copyright (C) 2026 SIUR contributors.
 * SPDX-License-Identifier: GPL-2.0-only
 */
package es.siur.geoserver.gwc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.lang.reflect.Field;
import java.math.BigInteger;
import java.util.Map;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import org.geoserver.gwc.ConfigurableQuotaStore;
import org.geoserver.gwc.ConfigurableQuotaStoreProvider;
import org.geowebcache.diskquota.DiskQuotaMonitor;
import org.geowebcache.diskquota.jdbc.HSQLDialect;
import org.geowebcache.diskquota.jdbc.JDBCQuotaStore;
import org.geowebcache.diskquota.storage.Quota;
import org.junit.jupiter.api.Test;

class QuotaHealthControllerTest {

    @Test
    void provesRunningHsqlStoreWithRealRead() throws Exception {
        ConfigurableQuotaStoreProvider provider = mock(ConfigurableQuotaStoreProvider.class);
        DiskQuotaMonitor monitor = mock(DiskQuotaMonitor.class);
        ConfigurableQuotaStore wrapper = mock(ConfigurableQuotaStore.class);
        JDBCQuotaStore delegate = mock(JDBCQuotaStore.class);
        when(provider.getQuotaStore()).thenReturn(wrapper);
        when(provider.getException()).thenReturn(null);
        when(wrapper.getStore()).thenReturn(delegate);
        when(delegate.getDialect()).thenReturn(new HSQLDialect());
        when(wrapper.getGloballyUsedQuota()).thenReturn(new Quota(BigInteger.valueOf(4096)));
        when(monitor.isEnabled()).thenReturn(true);
        when(monitor.isRunning()).thenReturn(true);
        when(monitor.getQuotaStore()).thenReturn(wrapper);
        ScheduledThreadPoolExecutor executor = scheduledExecutor(monitor);
        try {
            Map<String, Object> result = new QuotaHealthController(provider, monitor).health();
            assertTrue((Boolean) result.get("healthy"));
            assertEquals("4096", result.get("global_used_bytes"));
            assertEquals("org.geowebcache.diskquota.jdbc.JDBCQuotaStore", result.get("store_class"));
        } finally {
            executor.shutdownNow();
        }
    }

    @Test
    void rejectsProviderFallbackOrStoppedScheduler() throws Exception {
        ConfigurableQuotaStoreProvider provider = mock(ConfigurableQuotaStoreProvider.class);
        DiskQuotaMonitor monitor = mock(DiskQuotaMonitor.class);
        when(provider.getException()).thenReturn(new IllegalStateException());
        when(monitor.isEnabled()).thenReturn(true);
        when(monitor.isRunning()).thenReturn(true);
        Map<String, Object> result = new QuotaHealthController(provider, monitor).health();
        assertFalse((Boolean) result.get("healthy"));
        assertFalse((Boolean) result.get("scheduled_cleanup_active"));
        assertTrue((Boolean) result.get("provider_error"));
    }

    private static ScheduledThreadPoolExecutor scheduledExecutor(DiskQuotaMonitor monitor) throws Exception {
        ScheduledThreadPoolExecutor executor = new ScheduledThreadPoolExecutor(1);
        executor.scheduleAtFixedRate(() -> {}, 1, 10, TimeUnit.SECONDS);
        Field field = DiskQuotaMonitor.class.getDeclaredField("cleanUpExecutorService");
        field.setAccessible(true);
        field.set(monitor, executor);
        return executor;
    }
}
