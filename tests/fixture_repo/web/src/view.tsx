import type { User } from './types';
import { toName } from './api';

export function UserCard({ user }: { user: User }) {
  return <div className="card">{toName(user)}</div>;
}
