import argparse
import base64
import dataclasses
import http
import logging
import os
import subprocess
import tarfile
import tempfile

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
import oci.client

import k8s.util
import odg_operator.odg_model as odgm


ci.log.configure_default_logging()
logger = logging.getLogger(__name__)
own_dir = os.path.abspath(os.path.dirname(__file__))


@dataclasses.dataclass
class ExtensionTemplate:
    cfg: dict
    helm_chart_ref: str
    type: str
    namespace: str
    base_url: str


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kubeconfig')
    parsed = parser.parse_args()

    kubernetes_api = k8s.util.kubernetes_api(kubeconfig_path=parsed.kubeconfig)
    resource_version = ''

    oci_client = oci.client.Client(credentials_lookup=lambda **kwargs: None)

    while True:
        group = odgm.ODGExtensionMeta.group
        plural = odgm.ODGExtensionMeta.plural
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
                namespace = metadata['namespace']

                extension_template = dacite.from_dict(
                    data={**event['object']['spec']},
                    data_class=ExtensionTemplate,
                )

                if event['type'] == 'DELETED':
                    kubernetes_api.custom_kubernetes_api.delete_namespaced_custom_object(
                        group=odgm.ManagedResourceMeta.group,
                        version=odgm.ManagedResourceMeta.version,
                        plural=odgm.ManagedResourceMeta.plural,
                        namespace=namespace,
                        name=extension_template.type,
                    )
                    kubernetes_api.core_kubernetes_api.delete_namespaced_secret(
                        namespace=namespace,
                        name=extension_template.type,
                    )
                    continue

                manifest = oci_client.manifest(image_reference=extension_template.helm_chart_ref)
                for layer in manifest.layers:
                    if layer.mediaType == 'application/vnd.cncf.helm.chart.content.v1.tar+gzip':
                        break
                else:
                    raise ValueError('helm chart layer not found')

                res = oci_client.blob(
                    image_reference=extension_template.helm_chart_ref,
                    digest=layer.digest,
                )

                tmpfile = tempfile.NamedTemporaryFile()
                for chunk in res.iter_content(chunk_size=4096):
                    tmpfile.write(chunk)
                tmpfile.seek(0)

                tmpdir = tempfile.TemporaryDirectory()

                with tarfile.open(fileobj=tmpfile, mode='r:gz') as tf:
                    tf.extractall(path=tmpdir.name, filter='fully_trusted')
                tmpfile.close()

                helm_path = os.path.join(tmpdir.name, extension_template.type)

                if extension_template.type == odgm.ExtensionTypes.DELIVERY_DB:
                    helm_path = os.path.join(tmpdir.name, 'postgresql')

                if extension_template.type in (
                    odgm.ExtensionTypes.ARTEFACT_ENUMERATOR,
                    odgm.ExtensionTypes.BACKLOG_CONTROLLER,
                    odgm.ExtensionTypes.MALWARE_SCANNER,
                ):
                    helm_path = os.path.join(tmpdir.name, 'extensions')

                values_path = os.path.join(helm_path, 'values.yaml')
                values = ci.util.parse_yaml_file(values_path)
                values = ci.util.merge_dicts(values, extension_template.cfg)

                yaml.dump(values, open(values_path, 'w'))

                completed_process = subprocess.run([
                    'helm',
                    'template',
                    '--include-crds',
                    helm_path,
                    '-f',
                    os.path.join(helm_path, 'values.yaml'),
                ], capture_output=True, text=True)
                manifests_raw = completed_process.stdout
                manifests = list(yaml.safe_load_all(manifests_raw))

                # ingress-nginx chart can generate empty manifests, omit them ...
                manifests = [
                    manifest
                    for manifest in manifests
                    if manifest
                ]

                for manifest in manifests:
                    if manifest['kind'] in (
                        'ReplicaSet',
                        'Deployment',
                    ):
                        manifest['metadata']['annotations'] = manifest['metadata'].get('annotations', {}) # noqa: E501
                        manifest['metadata']['annotations']['resources.gardener.cloud/preserve-replicas'] = "true" # noqa: E501

                # create mr if not existing
                try:
                    kubernetes_api.custom_kubernetes_api.get_namespaced_custom_object(
                        group=odgm.ManagedResourceMeta.group,
                        version=odgm.ManagedResourceMeta.version,
                        plural=odgm.ManagedResourceMeta.plural,
                        namespace=namespace,
                        name=extension_template.type,
                    )
                except kubernetes.client.exceptions.ApiException as e:
                    if e.status != http.HTTPStatus.NOT_FOUND:
                        raise

                    kubernetes_api.custom_kubernetes_api.create_namespaced_custom_object(
                        group=odgm.ManagedResourceMeta.group,
                        version=odgm.ManagedResourceMeta.version,
                        plural=odgm.ManagedResourceMeta.plural,
                        namespace=namespace,
                        body={
                            'apiVersion': odgm.ManagedResourceMeta.apiVersion(),
                            'kind': odgm.ManagedResourceMeta.kind,
                            'metadata': {
                                'name': extension_template.type,
                                'namespace': namespace,
                            },
                            'spec': {
                                'class': odgm.ManagedResourceClasses.EXTERNAL,
                                'keepObjects': False,
                                'secretRefs': [
                                    {
                                        'name': extension_template.type,
                                    }
                                ],
                            }
                        },
                    )

                secret_data = {
                    'data.yaml': base64.b64encode(
                        yaml.dump_all(manifests).encode()
                    ).decode(),
                }
                secret_metadata = kubernetes.client.V1ObjectMeta(
                    name=extension_template.type,
                    namespace=namespace,
                )
                secret_body = kubernetes.client.V1Secret(
                    api_version='v1',
                    kind='Secret',
                    metadata=secret_metadata,
                    data=secret_data,
                )

                try:
                    kubernetes_api.core_kubernetes_api.create_namespaced_secret(
                        namespace=namespace,
                        body=secret_body,
                    )
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 409:
                        # secret already exists, update instead
                        kubernetes_api.core_kubernetes_api.patch_namespaced_secret(
                            name=extension_template.type,
                            namespace=namespace,
                            body=secret_body,
                        )
                    else:
                        raise

                tmpdir.cleanup()

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
