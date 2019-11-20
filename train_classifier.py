import torch
import tqdm
import datetime
import os
import pickle
import time
import numpy as np
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
import json
import codecs

from config import get_classify_config
from solver import Solver
from utils.set_seed import seed_torch
from models.build_model import PrepareModel
from datasets.datasets import GetDataloader
from losses.get_loss import Loss
from utils.draw_confusion_matrix import plot_confusion_matrix


class TrainVal:
    def __init__(self, config, fold):
        self.config = config
        self.fold = fold
        self.epoch = config.epoch
        self.num_classes = config.num_classes
        print('USE LOSS: {}'.format(config.loss_name))

        # 加载模型
        prepare_model = PrepareModel()
        self.model = prepare_model.create_model(
            model_type=config.model_type,
            classes_num=self.num_classes
        )
        if torch.cuda.is_available():
            self.model = torch.nn.DataParallel(self.model)
            self.model = self.model.cuda()

        # 加载优化器
        self.optimizer = prepare_model.create_optimizer(config.model_type, self.model, config)

        # 加载衰减策略
        self.exp_lr_scheduler = prepare_model.create_lr_scheduler(
            config.lr_scheduler,
            self.optimizer,
            step_size=config.lr_step_size,
            restart_step=config.epoch,
        )

        # 加载损失函数
        self.criterion = Loss(config.model_type, config.loss_name, self.num_classes)

        # 实例化实现各种子函数的 solver 类
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.solver = Solver(self.model, self.device)

        # log初始化
        self.writer, self.time_stamp = self.init_log()
        self.model_path = os.path.join(self.config.save_path, self.config.model_type, self.time_stamp)

    def train(self, train_loader, valid_loader):
        """ 完成模型的训练，保存模型与日志
        Args:
            train_loader: 训练数据的DataLoader
            valid_loader: 验证数据的Dataloader
        """
        global_step = 0
        max_accuracy_valid = 0
        for epoch in range(self.epoch):
            self.model.train()
            epoch += 1
            images_number, epoch_corrects = 0, 0

            tbar = tqdm.tqdm(train_loader)
            for i, (images, labels) in enumerate(tbar):
                # 网络的前向传播与反向传播
                labels_predict = self.solver.forward(images)
                loss = self.solver.cal_loss(labels_predict, labels, self.criterion)
                self.solver.backword(self.optimizer, loss)

                images_number += images.size(0)
                epoch_corrects += self.model.module.get_classify_result(labels_predict, labels, self.device).sum()
                train_acc_iteration = self.model.module.get_classify_result(labels_predict, labels, self.device).mean()

                # 保存到tensorboard，每一步存储一个
                descript = self.criterion.record_loss_iteration(self.writer.add_scalar, global_step + i)
                self.writer.add_scalar('TrainAccIteration', train_acc_iteration, global_step + i)

                params_groups_lr = str()
                for group_ind, param_group in enumerate(self.optimizer.param_groups):
                    params_groups_lr = params_groups_lr + 'params_group_%d' % group_ind + ': %.12f, ' % param_group[
                        'lr']

                descript = '[Train][epoch: {}/{}][Lr :{}][Acc: {:.4f}]'.format(epoch, self.epoch,
                                                                               params_groups_lr,
                                                                               train_acc_iteration) + descript

                tbar.set_description(desc=descript)

            # 每一个epoch完毕之后，执行学习率衰减
            self.exp_lr_scheduler.step()
            global_step += len(train_loader)

            # 写到tensorboard中
            epoch_acc = epoch_corrects / images_number
            self.writer.add_scalar('TrainAccEpoch', epoch_acc, epoch)
            self.writer.add_scalar('Lr', self.exp_lr_scheduler.get_lr()[0], epoch)
            descript = self.criterion.record_loss_epoch(len(train_loader), self.writer.add_scalar, epoch)

            # Print the log info
            print('[Finish epoch: {}/{}][Average Acc: {:.4}]'.format(epoch, self.epoch, epoch_acc) + descript)

            # 验证模型
            val_accuracy, val_loss = self.validation(valid_loader, epoch)

            if val_accuracy > max_accuracy_valid:
                is_best = True
                max_accuracy_valid = val_accuracy
            else:
                is_best = False

            state = {
                'epoch': epoch,
                'state_dict': self.model.module.state_dict(),
                'max_score': max_accuracy_valid
            }
            self.solver.save_checkpoint(
                os.path.join(
                    self.model_path,
                    '%s_fold%d.pth' % (self.config.model_type, self.fold)
                ),
                state,
                is_best
            )
            self.writer.add_scalar('ValidLoss', val_loss, epoch)
            self.writer.add_scalar('ValidAccuracy', val_accuracy, epoch)

    def validation(self, valid_loader, epoch):
        if epoch == self.epoch:  # TODO 只保留最后一个epoch的混淆矩阵图
            save_result = True
        else:
            save_result = False

        tbar = tqdm.tqdm(valid_loader)
        self.model.eval()
        labels_predict_all, labels_all = np.empty(shape=(0, )), np.empty(shape=(0, ))
        epoch_loss = 0
        with torch.no_grad():
            for i, (images, labels) in enumerate(tbar):
                # 网络的前向传播
                labels_predict = self.solver.forward(images)
                loss = self.solver.cal_loss(labels_predict, labels, self.criterion)

                epoch_loss += loss

                # 先经过softmax函数，再经过argmax函数
                labels_predict = F.softmax(labels_predict, dim=1)
                labels_predict = torch.argmax(labels_predict, dim=1).detach().cpu().numpy()

                labels_predict_all = np.concatenate((labels_predict_all, labels_predict))
                labels_all = np.concatenate((labels_all, labels))

                descript = '[Valid][Loss: {:.4f}]'.format(loss)
                tbar.set_description(desc=descript)

            acc_for_each_class, oa, average_accuracy, kappa = plot_confusion_matrix(
                labels_all,
                labels_predict_all,
                list(range(self.num_classes)),
                self.model_path,
                save_result=save_result
            )
            print('OA:{}, AA:{}, Kappa:{}'.format(oa, average_accuracy, kappa))

            return oa, epoch_loss/len(tbar)

    def init_log(self):
        # 保存配置信息和初始化tensorboard
        TIMESTAMP = "log-{0:%Y-%m-%dT%H-%M-%S}".format(datetime.datetime.now())
        log_dir = os.path.join(self.config.save_path, self.config.model_type, TIMESTAMP)
        writer = SummaryWriter(log_dir=log_dir)
        with codecs.open(os.path.join(log_dir, 'config.json'), 'w', "utf-8") as json_file:
            json.dump({k: v for k, v in config._get_kwargs()}, json_file, ensure_ascii=False)

        seed = int(time.time())
        seed_torch(seed)
        with open(os.path.join(log_dir, 'seed.pkl'), 'wb') as f:
            pickle.dump({'seed': seed}, f, -1)

        return writer, TIMESTAMP


if __name__ == "__main__":
    data_root = 'data/huawei_data/train_data'
    folds_split = 1
    test_size = 0.2
    mean = (0.485, 0.456, 0.406)
    std = (0.229, 0.224, 0.225)
    config = get_classify_config()
    get_dataloader = GetDataloader(data_root, folds_split=1, test_size=test_size)
    train_dataloaders, val_dataloaders = get_dataloader.get_dataloader(config.batch_size, config.image_size, mean, std,
                                                                       transforms=None)

    for fold_index, [train_loader, valid_loader] in enumerate(zip(train_dataloaders, val_dataloaders)):
        train_val = TrainVal(config, fold_index)
        train_val.train(train_loader, valid_loader)