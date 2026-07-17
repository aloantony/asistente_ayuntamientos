type NavigationGuard = () => boolean | Promise<boolean>;

const navigationGuards = new Set<NavigationGuard>();

export function registerNavigationGuard(guard: NavigationGuard) {
  navigationGuards.add(guard);
  return () => {
    navigationGuards.delete(guard);
  };
}

export async function runNavigationGuards() {
  for (const guard of [...navigationGuards]) {
    try {
      if (!(await guard())) {
        return false;
      }
    } catch {
      return false;
    }
  }
  return true;
}
