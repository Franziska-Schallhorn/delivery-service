import dataclasses
import enum


class ManagedResourceClasses(enum.StrEnum):
    INTERNAL = 'internal'
    EXTERNAL = 'external'


class ExtensionTypes(enum.StrEnum):
    DELIVERY_SERVICE = 'delivery-service'
    DELIVERY_DASHBOARD = 'delivery-dashboard'
    DELIVERY_DB = 'delivery-db'
    MALWARE_SCANNER = 'malware-scanner'
    ARTEFACT_ENUMERATOR = 'artefact-enumerator'
    BACKLOG_CONTROLLER = 'backlog-controller'
    INGRESS_NGINX = 'ingress-nginx'


@dataclasses.dataclass
class ManagedResourceMeta:
    group: str = 'resources.gardener.cloud'
    version: str = 'v1alpha1'
    plural: str = 'managedresources'
    kind: str = 'ManagedResource'

    @staticmethod
    def apiVersion() -> str:
        return f'{ManagedResourceMeta.group}/{ManagedResourceMeta.version}'


@dataclasses.dataclass
class ODGExtensionMeta:
    group: str = 'open-delivery-gear.ocm.software'
    version: str = 'v1'
    plural: str = 'odges'
    kind: str = 'ODGE'

    @staticmethod
    def apiVersion() -> str:
        return f'{ODGExtensionMeta.group}/{ODGExtensionMeta.version}'


@dataclasses.dataclass
class ODGMeta:
    group: str = 'open-delivery-gear.ocm.software'
    version: str = 'v1'
    plural: str = 'odgs'
    kind: str = 'ODG'

    @staticmethod
    def apiVersion() -> str:
        return f'{ODGMeta.group}/{ODGMeta.version}'


@dataclasses.dataclass
class InstallationTarget:
    namespace: str


@dataclasses.dataclass
class InstallationOrigin:
    namespace: str


@dataclasses.dataclass
class ExtensionMeta:
    managed_resource_name: str
    secret_name: str


@dataclasses.dataclass
class Extension:
    type: ExtensionTypes
    base_url: str
    meta: ExtensionMeta
    cfg: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Installation:
    target: InstallationTarget
    origin: InstallationOrigin
    extensions: list[Extension]
    odg_version: str
