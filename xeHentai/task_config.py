from collections.abc import MutableMapping
from typing import Any, Dict, Iterator, Mapping, Optional


class TaskConfig(MutableMapping[str, Any]):
	"""Task-level config view with parent fallback."""

	def __init__(self, local: Optional[Mapping[str, Any]] = None, parent: Optional[Mapping[str, Any]] = None):
		self._local: Dict[str, Any] = dict(local or {})
		self._parent = parent

	@property
	def parent(self) -> Optional[Mapping[str, Any]]:
		return self._parent

	def __getitem__(self, key: str) -> Any:
		if key in self._local:
			return self._local[key]
		if self._parent is not None:
			return self._parent[key]
		raise KeyError(key)

	def __setitem__(self, key: str, value: Any) -> None:
		self._local[key] = value

	def __delitem__(self, key: str) -> None:
		if key not in self._local:
			raise KeyError(key)
		del self._local[key]

	def __iter__(self) -> Iterator[str]:
		return iter(self.to_dict())

	def __len__(self) -> int:
		return len(self.to_dict())

	def __contains__(self, key: object) -> bool:
		return key in self._local or (self._parent is not None and key in self._parent)

	def get(self, key: str, default: Any = None) -> Any:
		try:
			return self[key]
		except KeyError:
			return default

	def update(self, other: Optional[Mapping[str, Any]] = None, **kwargs: Any) -> None:
		if other:
			if hasattr(other, 'items'):
				for key, value in other.items():
					self._local[key] = value
			else:
				for key, value in other:
					self._local[key] = value
		for key, value in kwargs.items():
			self._local[key] = value

	def to_local_dict(self) -> Dict[str, Any]:
		return dict(self._local)

	def to_dict(self) -> Dict[str, Any]:
		merged: Dict[str, Any] = {}
		if self._parent is not None:
			if hasattr(self._parent, 'to_dict'):
				merged.update(self._parent.to_dict())
			else:
				merged.update(dict(self._parent))
		merged.update(self._local)
		return merged

	def copy(self) -> Dict[str, Any]:
		return self.to_dict()

	def __repr__(self) -> str:
		return repr(self.to_dict())


class CoreConfig(dict):
	"""Core config mapping that can spawn task-level config views."""

	def create_task_config(self, overrides: Optional[Mapping[str, Any]] = None) -> TaskConfig:
		if isinstance(overrides, TaskConfig):
			overrides = overrides.to_local_dict()
		return TaskConfig(overrides, parent=self)