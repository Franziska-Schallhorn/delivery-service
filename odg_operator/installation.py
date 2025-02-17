import dataclasses


DOMAIN = 'open-delivery-gear.ocm.software'
LABEL_SERVICE = f'{DOMAIN}/installation'


@dataclasses.dataclass
class InstallationCRD:
    DOMAIN: str = DOMAIN
    VERSION: str = 'v1'
    KIND: str = 'Installation'
    PLURAL_NAME: str = 'installations'

    def api_version(self):
        return f'{self.DOMAIN}/{self.VERSION}'
