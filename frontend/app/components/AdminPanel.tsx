import { GroupsAdmin, type GroupsAdminProps } from "./GroupsAdmin";
import {
  MunicipalitiesAdmin,
  type MunicipalitiesAdminProps,
} from "./MunicipalitiesAdmin";
import {
  OrganizationsAdmin,
  type OrganizationsAdminProps,
} from "./OrganizationsAdmin";
import {
  OrdinancesAdmin,
  type OrdinancesAdminProps,
} from "./OrdinancesAdmin";
import { RbacAdmin, type RbacAdminProps } from "./RbacAdmin";
import { UsersAdmin, type UsersAdminProps } from "./UsersAdmin";
import {
  formatUserOption,
  userHasPermission,
  type Group,
  type MembershipAction,
} from "./types";

type AdminPanelProps = UsersAdminProps &
  OrganizationsAdminProps &
  MunicipalitiesAdminProps &
  OrdinancesAdminProps &
  RbacAdminProps &
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
    currentUser,
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
  const canManageOrganizations = userHasPermission(
    currentUser,
    "organizations.manage",
  );
  const canManageUsers = userHasPermission(currentUser, "users.manage");
  const canManageGroups = userHasPermission(currentUser, "groups.manage");
  const canManageRoles = userHasPermission(currentUser, "roles.manage");
  const canUseMunicipalities = [
    "municipalities.view",
    "municipalities.create",
    "municipalities.edit",
    "municipalities.archive",
    "municipalities.manage",
  ].some((permissionCode) => userHasPermission(currentUser, permissionCode));
  const canUseOrdinances = [
    "ordinances.view",
    "ordinances.create",
    "ordinances.edit",
    "ordinances.archive",
    "ordinances.manage",
  ].some((permissionCode) => userHasPermission(currentUser, permissionCode));

  return (
    <section className="panel admin-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Administración</p>
          <h2>Administración</h2>
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

      {canUseMunicipalities ? <MunicipalitiesAdmin {...props} /> : null}
      {canUseOrdinances ? <OrdinancesAdmin {...props} /> : null}
      {canManageOrganizations ? <OrganizationsAdmin {...props} /> : null}
      {canManageUsers ? <UsersAdmin {...props} /> : null}
      {canManageGroups ? <GroupsAdmin {...props} /> : null}
      {canManageRoles ? <RbacAdmin {...props} /> : null}

      {canManageGroups ? (
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
                  {group.name} - {group.organization.name}
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
      ) : null}
    </section>
  );
}
