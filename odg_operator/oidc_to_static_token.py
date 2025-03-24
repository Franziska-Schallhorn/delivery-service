import argparse
import base64
import logging
import os

import kubernetes.client
import kubernetes.client.models.authentication_v1_token_request as tq
import kubernetes.client.rest
import yaml

import ci.log

import k8s.util
import odg_operator.odg_model as odgm


ci.log.configure_default_logging()
logger = logging.getLogger(__name__)
CUSTOMER_CLUSTER_SERVICE_ACCOUNT_NAME = 'odg-serviceuser'


def main(kubeconfig: dict | None):
    oidc_token_path = os.path.abspath('/var/run/secrets/tokens/oidc-token')
    with open(oidc_token_path) as f:
        oidc_token = f.read().strip()

    api_key = {'authorization': f'Bearer {oidc_token}'}
    namespace = os.environ['CUSTOMER_NAMESPACE']

    own_k8s_client = k8s.util.kubernetes_api(kubeconfig_path=kubeconfig)

    odgs = own_k8s_client.custom_kubernetes_api.list_namespaced_custom_object(
            group=odgm.ODGMeta.group,
            version=odgm.ODGMeta.version,
            plural=odgm.ODGMeta.plural,
            namespace=namespace,
    )

    odg = odgs['items'][0]
    customer_namespace = odg['spec']['namespace']
    kubeconfig_secret_name = odg['spec']['target_kubeconfig_secret_name']
    hostname = odg['spec']['api_server_url']
    ca_crt = odg['spec']['api_server_ca']

    import tempfile
    tf = tempfile.NamedTemporaryFile(mode='w')

    with open(tf.name, 'w') as f:
        f.write(ca_crt)

    k8s_client_cfg = kubernetes.client.Configuration()
    k8s_client_cfg.host = hostname
    k8s_client_cfg.ssl_ca_cert = tf.name
    k8s_client_cfg.api_key = api_key

    customer_k8s_client = k8s.util.kubernetes_api(
        kubernetes_client_cfg=k8s_client_cfg,
    )

    # create service account if not present
    try:
        sa = customer_k8s_client.core_kubernetes_api.read_namespaced_service_account(
            name=CUSTOMER_CLUSTER_SERVICE_ACCOUNT_NAME,
            namespace=customer_namespace,
        )
    except kubernetes.client.rest.ApiException as e:
        if e.status != 404:
            raise

        customer_k8s_client.core_kubernetes_api.create_namespaced_service_account(
            namespace=customer_namespace,
            body={
                'metadata': {
                    'name': CUSTOMER_CLUSTER_SERVICE_ACCOUNT_NAME,
                }
            }
        )

    # refresh token
    token_request: tq = customer_k8s_client.core_kubernetes_api.create_namespaced_service_account_token(
        name=CUSTOMER_CLUSTER_SERVICE_ACCOUNT_NAME,
        namespace=customer_namespace,
        body={}
    )
    token = token_request.status.token

    secret_data = {
        'apiVersion': 'v1',
        'kind': 'Config',
        'clusters': [
            {
                'name': 'kubernetes',
                'cluster': {
                    'server': hostname,
                    'certificate-authority-data': base64.b64encode(ca_crt.encode()).decode(),
                }
            }
        ],
        'contexts': [
            {
                'name': 'default',
                'context': {
                    'cluster': 'kubernetes',
                    'user': 'default',
                    'namespace': customer_namespace,
                }
            }
        ],
        'current-context': 'default',
        'users': [
            {
                'name': 'default',
                'user': {
                    'token': token,
                }
            }
        ]
    }
    secret_data = {
        'data.yaml': base64.b64encode(
            yaml.dump(secret_data).encode()
        ).decode(),
    }

    secret_metadata = kubernetes.client.V1ObjectMeta(
        name=kubeconfig_secret_name,
        namespace=customer_namespace,
    )
    secret_body = kubernetes.client.V1Secret(
        api_version='v1',
        kind='Secret',
        metadata=secret_metadata,
        data=secret_data,
    )

    try:
        own_k8s_client.core_kubernetes_api.create_namespaced_secret(
            namespace=customer_namespace,
            body=secret_body,
        )
        logger.info('kubeconfig secret created')
    except kubernetes.client.rest.ApiException as e:
        if e.status == 409:
            # secret already exists, update instead
            own_k8s_client.core_kubernetes_api.patch_namespaced_secret(
                name=kubeconfig_secret_name,
                namespace=customer_namespace,
                body=secret_body,
            )
            logger.info('kubeconfig secret updated')
        else:
            raise

    tf.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--kubeconfig')
    parsed = parser.parse_args()
    main(kubeconfig=parsed.kubeconfig)
