/*
 * Copyright (C) 2026 SIUR contributors.
 * SPDX-License-Identifier: GPL-2.0-only
 */
package es.siur.geoserver.gwc;

import java.lang.reflect.Field;
import java.math.BigInteger;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import org.geoserver.gwc.ConfigurableQuotaStore;
import org.geoserver.gwc.ConfigurableQuotaStoreProvider;
import org.geowebcache.diskquota.DiskQuotaMonitor;
import org.geowebcache.diskquota.QuotaStore;
import org.geowebcache.diskquota.jdbc.JDBCQuotaStore;
import org.geowebcache.diskquota.storage.Quota;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Version-pinned, read-only proof that GWC is enforcing quota through the real HSQL store. The stock REST configuration
 * cannot distinguish the silent DummyQuotaStore fallback.
 */
@RestController
@RequestMapping(path = "/rest/siur/gwc-quota-health")
public final class QuotaHealthController {

    private static final String JDBC_STORE = "org.geowebcache.diskquota.jdbc.JDBCQuotaStore";
    private static final String HSQL_DIALECT = "org.geowebcache.diskquota.jdbc.HSQLDialect";

    private final ConfigurableQuotaStoreProvider provider;
    private final DiskQuotaMonitor monitor;

    public QuotaHealthController(
            @Qualifier("DiskQuotaStoreProvider") ConfigurableQuotaStoreProvider provider,
            @Qualifier("DiskQuotaMonitor") DiskQuotaMonitor monitor) {
        this.provider = provider;
        this.monitor = monitor;
    }

    @GetMapping(produces = "application/json")
    public Map<String, Object> health() {
        boolean monitorEnabled = false;
        boolean monitorRunning = false;
        boolean scheduledCleanupActive = false;
        boolean providerError = true;
        String storeClass = null;
        String dialectClass = null;
        String globalUsedBytes = null;
        boolean healthy = false;
        try {
            monitorEnabled = monitor.isEnabled();
            monitorRunning = monitor.isRunning();
            scheduledCleanupActive = scheduledCleanupActive(monitor);
            providerError = provider.getException() != null;
            QuotaStore wrapper = provider.getQuotaStore();
            if (wrapper != null) {
                storeClass = wrapper.getClass().getName();
            }
            if (wrapper != null
                    && wrapper.getClass() == ConfigurableQuotaStore.class
                    && monitor.getQuotaStore() == wrapper) {
                QuotaStore delegate = ((ConfigurableQuotaStore) wrapper).getStore();
                if (delegate != null) {
                    storeClass = delegate.getClass().getName();
                }
                if (delegate != null && delegate.getClass() == JDBCQuotaStore.class) {
                    Object dialect = ((JDBCQuotaStore) delegate).getDialect();
                    if (dialect != null) {
                        dialectClass = dialect.getClass().getName();
                    }
                    Quota used = wrapper.getGloballyUsedQuota();
                    BigInteger bytes = used == null ? null : used.getBytes();
                    if (bytes != null && bytes.signum() >= 0) {
                        globalUsedBytes = bytes.toString();
                    }
                }
            }
            healthy = monitorEnabled
                    && monitorRunning
                    && scheduledCleanupActive
                    && !providerError
                    && JDBC_STORE.equals(storeClass)
                    && HSQL_DIALECT.equals(dialectClass)
                    && globalUsedBytes != null;
        } catch (Exception error) {
            healthy = false;
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("schema_version", 1);
        result.put("healthy", healthy);
        result.put("monitor_enabled", monitorEnabled);
        result.put("monitor_running", monitorRunning);
        result.put("scheduled_cleanup_active", scheduledCleanupActive);
        result.put("provider_error", providerError);
        result.put("store_class", storeClass);
        result.put("dialect_class", dialectClass);
        result.put("global_used_bytes", globalUsedBytes);
        return result;
    }

    private static boolean scheduledCleanupActive(DiskQuotaMonitor monitor) throws ReflectiveOperationException {
        Field field = DiskQuotaMonitor.class.getDeclaredField("cleanUpExecutorService");
        field.setAccessible(true);
        Object value = field.get(monitor);
        if (!(value instanceof ScheduledThreadPoolExecutor executor)) {
            return false;
        }
        return !executor.isShutdown()
                && !executor.isTerminated()
                && (executor.getActiveCount() > 0 || !executor.getQueue().isEmpty());
    }
}
