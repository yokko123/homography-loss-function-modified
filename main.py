import argparse
import os
import random

import numpy as np
import torch
import tqdm
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

import datasets
import losses
import models
from utils import batch_to_device, batch_errors, batch_compute_utils, log_poses, log_errors

if __name__ == '__main__':
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        'path', metavar='DATA_PATH',
        help='path to the dataset directory, e.g. "/home/data/KingsCollege"'
    )
    parser.add_argument(
        '--loss', help='loss function for training',
        choices=['local_homography', 'global_homography', 'posenet', 'homoscedastic', 'geometric', 'dsac'],
        default='local_homography'
    )
    parser.add_argument('--epochs', help='number of epochs for training', type=int, default=20)
    parser.add_argument('--batch_size', help='training batch size', type=int, default=16)
    parser.add_argument('--xmin_percentile', help='xmin depth percentile', type=float, default=0.025)
    parser.add_argument('--xmax_percentile', help='xmax depth percentile', type=float, default=0.975)
    parser.add_argument(
        '--weights', metavar='WEIGHTS_PATH',
        help='path to weights with which the model will be initialized'
    )
    parser.add_argument(
        '--device', default='cpu',
        help='set the device to train the model, `cuda` for GPU'
    )
    args = parser.parse_args()

    # Set seed for reproductibility
    seed = 1
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Load model
    model = models.load_model(args.weights)
    model.train()
    model.to(args.device)

    # Load dataset
    dataset_name = os.path.basename(os.path.normpath(args.path))
    if dataset_name in ['GreatCourt', 'KingsCollege', 'OldHospital', 'ShopFacade', 'StMarysChurch', 'Street']:
        dataset = datasets.CambridgeDataset(args.path, args.xmin_percentile, args.xmax_percentile)
    elif dataset_name in ['chess', 'fire', 'heads', 'office', 'pumpkin', 'redkitchen', 'stairs']:
        dataset = datasets.SevenScenesDataset(args.path, args.xmin_percentile, args.xmax_percentile)
    else:
        dataset = datasets.COLMAPDataset(args.path, args.xmin_percentile, args.xmax_percentile)

    # Wrapper for use with PyTorch's DataLoader
    train_dataset = datasets.RelocDataset(dataset.train_data)
    test_dataset = datasets.RelocDataset(dataset.test_data)

    # Creating data loaders for train and test data
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        pin_memory=True,
        collate_fn=datasets.collate_fn,
        drop_last=True
    )
    test_loader = DataLoader(
        test_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        pin_memory=True,
        collate_fn=datasets.collate_fn
    )

    # Adam optimizer default epsilon parameter is 1e-8
    eps = 1e-8

    # Instantiate loss
    if args.loss == 'local_homography':
        criterion = losses.LocalHomographyLoss(device=args.device)
        eps = 1e-14  # Adam optimizer epsilon is set to 1e-14 for homography losses
    elif args.loss == 'global_homography':
        criterion = losses.GlobalHomographyLoss(
            xmin=dataset.train_global_xmin,
            xmax=dataset.train_global_xmax,
            device=args.device
        )
        eps = 1e-14  # Adam optimizer epsilon is set to 1e-14 for homography losses
    elif args.loss == 'posenet':
        criterion = losses.PoseNetLoss(beta=500)
    elif args.loss == 'homoscedastic':
        criterion = losses.HomoscedasticLoss(s_hat_t=0.0, s_hat_q=-3.0, device=args.device)
    elif args.loss == 'geometric':
        criterion = losses.GeometricLoss()
    elif args.loss == 'dsac':
        criterion = losses.DSACLoss()
    else:
        raise Exception(f'Loss {args.loss} not recognized...')

    # Instantiate adam optimizer
    optimizer = torch.optim.Adam(list(model.parameters()) + list(criterion.parameters()), lr=1e-4, eps=eps)

    # Set up tensorboard
    writer = SummaryWriter(os.path.join('logs', os.path.basename(os.path.normpath(args.path)), args.loss))

    # Set up folder to save weights
    if not os.path.exists(os.path.join(writer.log_dir, 'weights')):
        os.makedirs(os.path.join(writer.log_dir, 'weights'))

    # Set up file to save logs
    log_file_path = os.path.join(writer.log_dir, 'epochs_poses_log.csv')
    with open(log_file_path, mode='w') as log_file:
        log_file.write('epoch,image_file,type,w_tx_chat,w_ty_chat,w_tz_chat,chat_qw_w,chat_qx_w,chat_qy_w,chat_qz_w\n')

    print('Start training...')
    for epoch in tqdm.tqdm(range(args.epochs)):
        epoch_loss = 0
        errors = {}

        for batch in train_loader:
            optimizer.zero_grad()

            # Move all batch data to proper device
            batch = batch_to_device(batch, args.device)

            # Estimate the pose from the image
            batch['w_t_chat'], batch['chat_q_w'] = model(batch['image']).split([3, 4], dim=1)

            # Computes useful data for our batch
            # - Normalized quaternion
            # - Rotation matrix from this normalized quaternion
            # - Reshapes translation component to fit shape (batch_size, 3, 1)
            batch_compute_utils(batch)

            # Compute loss
            loss = criterion(batch)

            # Backprop
            loss.backward()
            optimizer.step()

            # Add current batch loss to epoch loss
            epoch_loss += loss.item() / len(train_loader)

            # Compute training batch errors and log poses
            with torch.no_grad():
                batch_errors(batch, errors)

                with open(log_file_path, mode='a') as log_file:
                    log_poses(log_file, batch, epoch, 'train')

        # Log epoch loss
        writer.add_scalar('train loss', epoch_loss, epoch)

        with torch.no_grad():

            # Log train errors
            log_errors(errors, writer, epoch, 'train')

            # Set the model to eval mode for test data
            model.eval()
            errors = {}

            for batch in test_loader:
                # Compute test poses estimations
                batch = batch_to_device(batch, args.device)
                batch['w_t_chat'], batch['chat_q_w'] = model(batch['image']).split([3, 4], dim=1)
                batch_compute_utils(batch)

                # Log test poses
                with open(log_file_path, mode='a') as log_file:
                    log_poses(log_file, batch, epoch, 'test')

                # Compute test errors
                batch_errors(batch, errors)

            # Log test errors
            log_errors(errors, writer, epoch, 'test')

            # Log loss parameters, if there are any
            for p_name, p in criterion.named_parameters():
                writer.add_scalar(p_name, p, epoch)

            writer.flush()
            model.train()

            # Save model and optimizer weights every n and last epochs:
            if epoch % 500 == 0 or epoch == args.epochs - 1:
                torch.save({
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'criterion_state_dict': criterion.state_dict()
                }, os.path.join(writer.log_dir, 'weights', f'epoch_{epoch}.pth'))

    writer.close()
