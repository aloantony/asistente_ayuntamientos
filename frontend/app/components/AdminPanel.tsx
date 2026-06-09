import { GroupsAdmin, type GroupsAdminProps } from "./GroupsAdmin";
import { UsersAdmin, type UsersAdminProps } from "./UsersAdmin";
import { formatUserOption, type Group, type MembershipAction } from "./types";

type AdminPanelProps = UsersAdminProps &
  GroupsAdminProps & {
    adminError: string;
    membershipUserId: string;
    membershipGroupId: string;
    membershipError: string;
    membershipMessage: string;
    isUpdatingMembership: boolean;
    onRefresh: () => void;
    onMembershipUserIdChange: (userId: string) => void;
    onMembershipGroupIdChange: (groupId: string) => void;
    onUpdateMembership: (action: MembershipAction) => void;
  };

export function AdminPanel(props: AdminPanelProps) {
  const {
    adminUsers,
    groups,
    isLoadingAdmin,
    adminError,
    membershipUserId,
    membershipGroupId,
    membershipError,
    membershipMessage,
    isUpdatingMembership,
    onRefresh,
    onMembershipUserIdChange,
    onMembershipGroupIdChange,
    onUpdateMembership,
  } = props;

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Administración</p>
          <h2>Usuarios y grupos</h2>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={onRefresh}
          disabled={isLoadingAdmin}
        >
          {isLoadingAdmin ? "Cargando..." : "Actualizar"}
        </button>
      </div>

      {adminError ? <p className="error-message">{adminError}</p> : null}

      <UsersAdmin {...props} />
      <GroupsAdmin {...props} />

      <div className="admin-section">
        <h3>Pertenencia a grupos</h3>

        <div className="membership-controls">
          <label>
            Usuario
            <select
              onChange={(event) =>
                onMembershipUserIdChange(event.target.value)
              }
              value={membershipUserId}
            >
              <option value="">Selecciona un usuario</option>
              {adminUsers.map((adminUser) => (
                <option key={adminUser.id} value={adminUser.id}>
                  {formatUserOption(adminUser)}
                </option>
              ))}
            </select>
          </label>

          <label>
            Grupo
            <select
              onChange={(event) =>
                onMembershipGroupIdChange(event.target.value)
              }
              value={membershipGroupId}
            >
              <option value="">Selecciona un grupo</option>
              {groups.map((group: Group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {membershipError ? (
          <p className="error-message">{membershipError}</p>
        ) : null}
        {membershipMessage ? (
          <p className="success-message">{membershipMessage}</p>
        ) : null}

        <div className="button-row">
          <button
            type="button"
            onClick={() => onUpdateMembership("add")}
            disabled={isUpdatingMembership}
          >
            Añadir al grupo
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => onUpdateMembership("remove")}
            disabled={isUpdatingMembership}
          >
            Quitar del grupo
          </button>
        </div>
      </div>
    </section>
  );
}
