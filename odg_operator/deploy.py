import dataclasses
import logging
import os

import dacite
import kubernetes.utils
from kubernetes.utils.create_from_yaml import FailToCreateError
import yaml

import ci.log

import k8s.util


ci.log.configure_default_logging()
logger = logging.getLogger(__name__)
own_dir = os.path.dirname(os.path.abspath(__file__))
deployment_dir = os.path.join(own_dir, 'deployments')
deployment_cfg_path = os.path.join(own_dir, 'deployment-cfg.yaml')
delivery_service_manifest_path = os.path.join(deployment_dir, 'delivery-service.yaml')


@dataclasses.dataclass(frozen=True)
class DeliveryServiceCfg:
    image: str
    hostname: str
    namespace: str


@dataclasses.dataclass(frozen=True)
class DeploymentCfg:
    type: str
    disabled: bool
    cfg: DeliveryServiceCfg


def deploy(
    kubernetes_api: k8s.util.KubernetesApi,
    namespace: str,
    manifest: dict,
):
    kind = manifest['kind']
    name = manifest['metadata']['name']
    qualified_name = f'<{kind}: {name}>'

    logger.info(f'deploying {qualified_name} from manifest')
    try:
        kubernetes.utils.create_from_dict(
            k8s_client=kubernetes_api.api_client,
            data=manifest,
            namespace=namespace,
        )
    except FailToCreateError:
        logger.warning(f'unable to create resource {qualified_name}')



def deploy_delivery_service(
    delivery_service_cfg: DeliveryServiceCfg = None,
    /,
    *,
    kubernetes_api: k8s.util.KubernetesApi = None,
):
    with open(delivery_service_manifest_path, 'r') as f:
        manifests = list(yaml.safe_load_all(f))

    for manifest in manifests:
        kind = manifest['kind']

        if kind == 'RoleBinding':
            manifest['subjects'] = [
                dict(subject, namespace=delivery_service_cfg.namespace)
                for subject in manifest['subjects']
            ]
            continue

        if kind == 'Deployment':
            containers = manifest['spec']['template']['spec']['containers']
            def iter_env(env_raw: list[dict[str, str]]):
                for e in env_raw:
                    name = e['name']
                    if name != 'K8S_TARGET_NAMESPACE':
                        yield e
                    else:
                        yield {
                            'name': name,
                            'value': delivery_service_cfg.namespace,
                        }
            containers = [
                dict(
                    container,
                    image=delivery_service_cfg.image,
                    env=list(iter_env(container['env'])),
                )
                for container in containers
            ]
            manifest['spec']['template']['spec']['containers'] = containers
            continue

        if kind == 'Ingress':
            manifest['spec']['rules'] = [
                dict(rule, host=delivery_service_cfg.hostname)
                for rule in manifest['spec']['rules']
            ]

            manifest['spec']['tls'] = [
                dict(tls, hosts=[delivery_service_cfg.hostname])
                for tls in manifest['spec']['tls']
            ]
            continue

    for manifest in manifests:
        deploy(
            kubernetes_api=kubernetes_api,
            namespace=delivery_service_cfg.namespace,
            manifest=manifest,
        )


def main():
    import ctx_util
    secret_factory = ctx_util.secret_factory()
    cfg = secret_factory.kubernetes('ocm_gear_dev')

    deploy_func_for_type = {
        'delivery-service': deploy_delivery_service,
    }

    with open(deployment_cfg_path, 'r') as f:
        deployment_cfgs_raw = yaml.safe_load(f)

    deployment_cfgs = [
        dacite.from_dict(
            data=deployment_cfg_raw,
            data_class=DeploymentCfg,
            config=dacite.Config(
                strict=True,
            )
        )
        for deployment_cfg_raw in deployment_cfgs_raw['deployments']
    ]

    kubernetes_api = k8s.util.kubernetes_api(
        kubernetes_cfg=cfg,
    )
    for deployment_cfg in deployment_cfgs:
        deploy = deploy_func_for_type[deployment_cfg.type]
        deploy(
            deployment_cfg.cfg,
            kubernetes_api=kubernetes_api,
        )


main()