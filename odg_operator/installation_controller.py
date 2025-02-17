import http
import logging
import os
import pprint
import tarfile
import tempfile
import subprocess

import kubernetes.client
import kubernetes.config
import kubernetes.watch
import urllib3

import ci.util
import cnudie.iter
import ocm

import ctx_util
import lookups
import k8s.util
import odg_operator.installation


logger = logging.getLogger(__name__)
own_dir = os.path.abspath(os.path.dirname(__file__))


def install(
    name: str,
    namespace: str,
    kubernetes_api: k8s.util.KubernetesApi,
):
    installation_cr = kubernetes_api.custom_kubernetes_api.get_namespaced_custom_object(
        group=odg_operator.installation.InstallationCRD.DOMAIN,
        version=odg_operator.installation.InstallationCRD.VERSION,
        namespace=namespace,
        name=name,
    )
    deployment_cfg = ci.util.parse_yaml_file(path=os.path.join(own_dir, 'deployment-cfg.yaml'))

    secret_ref = installation_cr['spec']['secretRef']
    secret = kubernetes_api.core_kubernetes_api.read_namespaced_secret(
        name=secret_ref['name'],
        namespace=namespace,
    )
    # TODO: oidc support
    '''
    host: <str> api-server url von trusted cluster
    ca-data: <obj> customer cluster CA
    audience: <str>
    '''

    target_kubernetes_api = k8s.util.kubernetes_api(kubernetes_cfg=secret['data'])
    target_namespace = installation_cr['spec']['namespace']

    extensions_cfg = target_kubernetes_api.core_kubernetes_api.read_namespaced_config_map(
        name='extensions-cfg',
        namespace=target_namespace,
    )['data']['extensions_cfg']

    findings_cfg = target_kubernetes_api.core_kubernetes_api.read_namespaced_config_map(
        name='findings-cfg',
        namespace=target_namespace,
    )['data']['findings_cfg']

    ocm_repo_mappings_cfg = target_kubernetes_api.core_kubernetes_api.read_namespaced_config_map(
        name='ocm-repo-mappings',
        namespace=target_namespace,
    )['data']['ocm_repo_mappings']

    secret_factory = ctx_util.secret_factory()
    oci_client = lookups.semver_sanitising_oci_client(secret_factory)
    component_descriptor_lookup = lookups.init_component_descriptor_lookup(
        oci_client=oci_client,
    )
    odg_cfg = kubernetes_api.core_kubernetes_api.read_namespaced_config_map(
        name='odg-cfg',
        namespace='odg',
    )
    odg_version = odg_cfg['data']['odg_version']
    odg_component: ocm.Component = component_descriptor_lookup(ocm.ComponentIdentity(
        name='ocm.software/ocm-gear',
        version=odg_version,
    )).component
    oci_ref = odg_component.current_ocm_repo.component_version_oci_ref()

    for rnode in cnudie.iter.iter(
        component=odg_component,
        recursion_depth=0,
        node_filter=cnudie.iter.Filter.resources,
    ):
        rnode: cnudie.iter.ResourceNode
        if rnode.resource.name == 'installation':
            break
    else:
        raise RuntimeError('installation resource not found')

    odg_installer = rnode.resource

    tmpfile = tempfile.TemporaryFile()

    for chunk in oci_client.blob(
        image_reference=oci_ref,
        digest=odg_installer.access.localReference,
    ).iter_content(chunk_size=4096):
        tmpfile.write(chunk)

    tmpfile.seek(0)

    tmpdir = tempfile.TemporaryDirectory()
    with tarfile.open(fileobj=tmpfile) as tf:
        tf.extractall(path=tmpdir)

    tmpfile.close()


    subprocess.call([
        os.path.join(tmpdir.name, 'install.sh'),
    ])


def main():
    kubernetes_api = k8s.util.kubernetes_api()

    resource_version = ''

    while True:
        try:
            for event in kubernetes.watch.Watch().stream(
                kubernetes_api.custom_kubernetes_api.list_namespaced_custom_object,
                group=odg_operator.installation.InstallationCRD.DOMAIN,
                version=odg_operator.installation.InstallationCRD.VERSION,
                #namespace=namespace,
                plural=odg_operator.installation.InstallationCRD.PLURAL_NAME,
                resource_version=resource_version,
                timeout_seconds=0,
            ):
                pprint.pprint(event)
                namespace = 'xxx'

                if not event['type'] in (
                    'CREATED',
                    'MODIFIED',
                ):
                    # TODO: delete (modified with deletion timestamp?)
                    logger.info(f'{event} not supported')
                    continue

                metadata = event['object'].get('metadata')
                resource_version = metadata['resourceVersion']
                name = metadata['name']

                logger.debug(f'identified modification {type=} of backlog item {name}')

                install(
                    namespace=namespace,
                    kubernetes_api=kubernetes_api,
                    name=name,
                )

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
