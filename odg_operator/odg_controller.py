import argparse
import base64
import enum
import http
import logging
import os

import dacite
import kubernetes.client
import kubernetes.client.exceptions
import kubernetes.client.rest
import kubernetes.config
import kubernetes.watch
import urllib3
import yaml

import ci.log
import ci.util
import cnudie.iter
import oci.client
import ocm

import k8s.util
import lookups
import odg_operator.odg_model as odgm


ci.log.configure_default_logging()
logger = logging.getLogger(__name__)
own_dir = os.path.abspath(os.path.dirname(__file__))


def delivery_dashboard_url(
    base_url: str,
) -> str:
    return f'modg-dashboard.{base_url}'


def delivery_service_url(
    base_url: str,
) -> str:
    return f'modg-service.{base_url}'


def fill_default_values(
    installation: odgm.Installation,
):
    for extension in installation.extensions:
        extension.cfg['target_namespace'] = installation.target.namespace

        if extension.type == odgm.ExtensionTypes.DELIVERY_DASHBOARD:
            extension.cfg = {
                'ingress': {
                    'hosts': [delivery_dashboard_url(extension.base_url)],
                },
                'envVars': {
                    'REACT_APP_DELIVERY_SERVICE_API_URL': f'https://{delivery_service_url(extension.base_url)}', # noqa: E501
                },
                'target_namespace': installation.target.namespace,
            }

        elif extension.type == odgm.ExtensionTypes.MALWARE_SCANNER:
            extension.cfg = {
                'clamav': {
                    'enabled': True,
                    'target_namespace': installation.target.namespace,
                }
            }


def default_extensions(
    base_url: str,
    target_namespace: str,
    delivery_db_password: str,
):
    return [
        odgm.Extension(
            type=odgm.ExtensionTypes.DELIVERY_SERVICE,
            base_url=base_url,
            meta=odgm.ExtensionMeta(
                managed_resource_name='delivery-service-odge',
                secret_name='delivery-service-odge',
            ),
            cfg={
                'ingress': {
                    'hosts': [delivery_service_url(base_url)],
                },
                'target_namespace': target_namespace,
            },
        ),
        odgm.Extension(
            type=odgm.ExtensionTypes.DELIVERY_DB,
            base_url=base_url,
            meta=odgm.ExtensionMeta(
                managed_resource_name='delivery-db-odge',
                secret_name='delivery-db-odge',
            ),
            cfg={
                'fullnameOverride': 'delivery-db',
                'namespaceOverride': target_namespace,
                'image': {
                    'tag': '16.0.0',
                },
                'auth': {
                    'postgresPassword': delivery_db_password,
                },
            },
        ),
        odgm.Extension(
            type=odgm.ExtensionTypes.ARTEFACT_ENUMERATOR,
            base_url=base_url,
            meta=odgm.ExtensionMeta(
                managed_resource_name='artefact-enumerator-odge',
                secret_name='artefact-enumerator-odge',
            ),
            cfg={
                'artefact-enumerator': {
                    'enabled': True,
                    'target_namespace': target_namespace,
                }
            },
        ),
        odgm.Extension(
            type=odgm.ExtensionTypes.BACKLOG_CONTROLLER,
            base_url=base_url,
            meta=odgm.ExtensionMeta(
                managed_resource_name='backlog-controller-odge',
                secret_name='backlog-controller-odge',
            ),
            cfg={
                'backlog-controller': {
                    'enabled': True,
                    'target_namespace': target_namespace,
                }
            },
        ),
        odgm.Extension(
            type=odgm.ExtensionTypes.INGRESS_NGINX,
            base_url=base_url,
            meta=odgm.ExtensionMeta(
                managed_resource_name='ingress-nginx-odge',
                secret_name='ingress-nginx-odge',
            ),
            cfg={
                'namespaceOverride': target_namespace,
                'externalTrafficPolicy': 'Cluster',
                'controller': {
                    'metrics': {
                        'enabled': True,
                    },
                    'podAnnotations': {
                        'prometheus.io/scrape': True,
                        'prometheus.io/port': '10254',
                    }
                }
            },
        ),
    ]


resource_name_for_extension_type = {
    odgm.ExtensionTypes.DELIVERY_SERVICE: 'delivery-service',
    odgm.ExtensionTypes.MALWARE_SCANNER: 'extensions',
    odgm.ExtensionTypes.BACKLOG_CONTROLLER: 'extensions',
    odgm.ExtensionTypes.ARTEFACT_ENUMERATOR: 'extensions',
    odgm.ExtensionTypes.DELIVERY_DASHBOARD: 'delivery-dashboard',
    odgm.ExtensionTypes.DELIVERY_DB: 'postgresql',
}


def helm_chart_for_extension(
    odg_version: str,
    extension: odgm.Extension,
) -> str:
    if extension.type == odgm.ExtensionTypes.INGRESS_NGINX:
        return 'europe-docker.pkg.dev/gardener-project/releases/charts/ocm-gear/ingress-nginx/ingress-nginx@sha256:f8296fc031beb8023b51e62c982a6c1c2f15e8584e4e70c36daf0885da830d2f'  # noqa: E501

    elif extension.type == odgm.ExtensionTypes.DELIVERY_DB:
        # use 16.5.3 already
        return 'europe-docker.pkg.dev/gardener-project/releases/charts/ocm-gear/postgresql/postgresql@sha256:516157e9547123d830af1b834020502e4ffd5e5c1af9f3713fcdcf88fda2923b'  # noqa: E501

    component_descriptor_lookup = lookups.init_component_descriptor_lookup(
        cache_dir='./cache/ocm',
        oci_client=oci.client.Client(
            credentials_lookup=lambda **kwargs: None,
        ),
    )
    odg_component = component_descriptor_lookup(f'ocm.software/ocm-gear:{odg_version}')

    for resource_node in cnudie.iter.iter(
        component=odg_component.component,
        lookup=component_descriptor_lookup,
        node_filter=cnudie.iter.Filter.resources,
    ):
        resource_node: cnudie.iter.ResourceNode
        if resource_node.resource.type != ocm.ArtefactType.HELM_CHART:
            continue

        if resource_node.resource.name != resource_name_for_extension_type[extension.type]:
            continue

        break

    else:
        raise ValueError(f'no helm chart found for {extension.type}')

    return resource_node.resource.access.imageReference


def create_or_update_deployment_secret(
    installation: odgm.Installation,
    kubernetes_api: k8s.util.KubernetesApi,
    extension: odgm.Extension,
):
    extension_secret = {
        'apiVersion': odgm.ODGExtensionMeta.apiVersion(),
        'kind': odgm.ODGExtensionMeta.kind,
        'metadata': {
            'name': extension.meta.secret_name,
            'namespace': installation.origin.namespace,
        },
        'spec': {
            'type': str(extension.type),
            'cfg': {
                **extension.cfg,
            },
            'namespace': installation.target.namespace,
            'base_url': extension.base_url,
            'helm_chart_ref': helm_chart_for_extension(
                odg_version=installation.odg_version,
                extension=extension,
            ),
        }
    }

    secret_data = {
        'data.yaml': base64.b64encode(
            yaml.dump(extension_secret).encode()
        ).decode(),
    }
    secret_metadata = kubernetes.client.V1ObjectMeta(
        name=extension.meta.secret_name,
        namespace=installation.origin.namespace,
    )
    secret_body = kubernetes.client.V1Secret(
        api_version='v1',
        kind='Secret',
        metadata=secret_metadata,
        data=secret_data,
    )

    try:
        kubernetes_api.core_kubernetes_api.create_namespaced_secret(
            namespace=installation.origin.namespace,
            body=secret_body,
        )
        logger.info('extension secret created')
    except kubernetes.client.rest.ApiException as e:
        if e.status == 409:
            # secret already exists, update instead
            kubernetes_api.core_kubernetes_api.patch_namespaced_secret(
                name=extension.meta.secret_name,
                namespace=installation.origin.namespace,
                body=secret_body,
            )
            logger.info('extension secret updated')
        else:
            raise


def create_managed_resource_if_absent(
    kubernetes_api: k8s.util.KubernetesApi,
    extension: odgm.Extension,
    namespace: str,
    managed_resource_class: str,
):
    try:
        kubernetes_api.custom_kubernetes_api.get_namespaced_custom_object(
            group=odgm.ManagedResourceMeta.group,
            version=odgm.ManagedResourceMeta.version,
            plural=odgm.ManagedResourceMeta.plural,
            namespace=namespace,
            name=extension.meta.managed_resource_name,
        )
    except kubernetes.client.exceptions.ApiException as e:
        if e.status != http.HTTPStatus.NOT_FOUND:
            raise

        logger.info('creating managed resource')
        kubernetes_api.custom_kubernetes_api.create_namespaced_custom_object(
            group=odgm.ManagedResourceMeta.group,
            version=odgm.ManagedResourceMeta.version,
            plural=odgm.ManagedResourceMeta.plural,
            namespace=namespace,
            body={
                'apiVersion': odgm.ManagedResourceMeta.apiVersion(),
                'kind': odgm.ManagedResourceMeta.kind,
                'metadata': {
                    'name': extension.meta.managed_resource_name,
                    'namespace': namespace,
                },
                'spec': {
                    'class': managed_resource_class,
                    'keepObjects': False,
                    'secretRefs': [
                        {
                            'name': extension.meta.secret_name,
                        }
                    ],
                }
            },
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kubeconfig')
    parsed = parser.parse_args()

    kubernetes_api = k8s.util.kubernetes_api(kubeconfig_path=parsed.kubeconfig)

    resource_version = ''

    while True:
        group = odgm.ODGExtensionMeta.group
        plural = 'odgs'
        logger.info(f'watching for events: {group=} {plural=}')
        try:
            for event in kubernetes.watch.Watch().stream(
                kubernetes_api.custom_kubernetes_api.list_cluster_custom_object,
                group=group,
                version='v1',
                plural=plural,
                resource_version=resource_version,
                timeout_seconds=0,
            ):
                logger.info('event received')
                metadata = event['object'].get('metadata')
                resource_version = metadata['resourceVersion']
                base_url = event['object']['spec']['base_url']
                delivery_db_password = event['object']['spec']['delivery_db_password']

                data = {
                    'target': {
                        'namespace': event['object']['spec']['namespace'],
                    },
                    'origin': {
                        'namespace': metadata['namespace'],
                    },
                    'extensions': [
                        {
                            **extension,
                            'base_url': base_url,
                            'meta': {
                                'managed_resource_name': f'{extension["type"]}-odge',
                                'secret_name': f'{extension["type"]}-odge',
                            }
                        }
                        for extension in event['object']['spec']['extensions']
                    ],
                    'odg_version': event['object']['spec']['version'],
                }
                installation = dacite.from_dict(
                    data_class=odgm.Installation,
                    data=data,
                    config=dacite.Config(cast=[enum.Enum])
                )
                installation.extensions.extend(
                    default_extensions(
                        base_url=base_url,
                        target_namespace=installation.target.namespace,
                        delivery_db_password=delivery_db_password,
                    )
                )
                fill_default_values(installation) # TODO: define extension-cfg classes (per type)

                for extension in installation.extensions:
                    if event['type'] == 'DELETED':
                        kubernetes_api.custom_kubernetes_api.delete_namespaced_custom_object(
                            group=odgm.ManagedResourceMeta.group,
                            version=odgm.ManagedResourceMeta.version,
                            plural=odgm.ManagedResourceMeta.plural,
                            namespace=installation.origin.namespace,
                            name=extension.meta.managed_resource_name,
                        )
                        kubernetes_api.core_kubernetes_api.delete_namespaced_secret(
                            namespace=installation.origin.namespace,
                            name=extension.meta.secret_name,
                        )
                        continue

                    elif event['type'] in ('ADDED', 'MODIFIED'):
                        create_managed_resource_if_absent(
                            kubernetes_api=kubernetes_api,
                            extension=extension,
                            namespace=installation.origin.namespace,
                            managed_resource_class=odgm.ManagedResourceClasses.INTERNAL,
                        )
                        create_or_update_deployment_secret(
                            kubernetes_api=kubernetes_api,
                            installation=installation,
                            extension=extension,
                        )

                    else:
                        logger.info(f'{event["type"]} not supported')
                        continue

        except kubernetes.client.rest.ApiException as e:
            if e.status == http.HTTPStatus.GONE:
                resource_version = ''
                logger.info('API resource watching expired, will start new watch')
            else:
                raise e

        except urllib3.exceptions.ProtocolError:
            # this is a known error which has no impact on the functionality, thus rather be
            # degregated to a warning or even info
            # [ref](https://github.com/kiwigrid/k8s-sidecar/issues/233#issuecomment-1332358459)
            resource_version = ''
            logger.info('API resource watching received protocol error, will start new watch')


if __name__ == '__main__':
    main()
