# Source Container Build

Used by [source build task](https://github.com/redhat-appstudio/build-definitions/tree/main/task/source-build) in Konflux pipelines.

## Build Image

Official images are built in GitHub CI job and only pushed to registry on GitHub
push event (when a pull request is merged).

## Testing

1. Make image: `IMAGE=$pullspec make image; podman push $pullspec`
2. Update `pullspec` to `path/to/build-definitions/task/source-build/0.1/source-build.yaml`
3. Apply this diff as a workaround of EC check:
    ```diff
             # https://issues.redhat.com/browse/EC-425
             - step_image_registries.step_images_permitted:tkn-bundle/0.1
    +        - step_image_registries.step_images_permitted:source-build/0.1
             # https://issues.redhat.com/browse/KFLUXBUGS-1110
             - step_image_registries.step_images_permitted:verify-signed-rpms/noversion
    ```
4. if there are corresponding changes made to e2e-tests:
    * build and push e2e-tests image with the changes
    * update the image to `path/to/build-definitions/.tekton/tasks/e2e-tests.yaml`
5. Open a `DONOTMERGE` pull request from `build-definitions`.

## Image update flow

1. Proposed changes are merged in this repository, then image is built and
   pushed to the registry.
2. Renovate detects the new image and sends an update pull request to
   [redhat-appstudio/build-definitions](https://github.com/redhat-appstudio/build-definitions/) repository.
3. Review and merge that pull request.
4. A new source build task bundle is built and pushed to registry.
5. Components repositories receive bundle update pull request, which is sent
   during a Build service Renovate controller reconciliation.

#Dummy change to trigger test
