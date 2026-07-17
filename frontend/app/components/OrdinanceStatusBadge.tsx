import { formatOrdinanceStatus, type OrdinanceStatus } from "./types";
import styles from "./OrdinanceLibrary.module.css";

const STATUS_CLASS: Record<OrdinanceStatus, string> = {
  active: styles.statusActive,
  partially_repealed: styles.statusCaution,
  unknown: styles.statusUnknown,
  repealed: styles.statusInactive,
  superseded: styles.statusInactive,
  archived: styles.statusInactive,
};

export function OrdinanceStatusBadge({ status }: { status: OrdinanceStatus }) {
  return (
    <span className={`${styles.statusBadge} ${STATUS_CLASS[status]}`}>
      {formatOrdinanceStatus(status)}
    </span>
  );
}
